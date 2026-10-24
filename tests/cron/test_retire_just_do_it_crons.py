from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass, replace
from datetime import datetime, timezone, UTC
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
PRE_MIGRATION_RETAINED_NAME = "just-do-it-review-reaction-state-watchdog"
POST_MIGRATION_RETAINED_NAME = "boris-review-reaction-state-watchdog"
ALL_CHANNEL_RETAINED_PROMPT = (
    "No-agent authoritative state machine for source-linked Boris reviewer lanes "
    "and historical legacy reviewer lanes. In #boris-reviews it maintains exactly "
    "one owned reaction with precedence face_vomiting > doge_notes > rubbing > "
    "blinkapproved. Legacy watched-channel reactions retain their prior vocabulary. "
    "Quiet when settled; emits compact transition, hold, reconciliation, or failure "
    "output. It never creates or mutates Kanban tasks. Do not schedule additional "
    "cron jobs."
)
BORIS_ONLY_RETAINED_PROMPT = (
    "No-agent authoritative state machine for source-linked Boris reviewer lanes. "
    "In #boris-reviews it maintains exactly one owned reaction with precedence "
    "face_vomiting > doge_notes > rubbing > blinkapproved. Scheduled/default "
    "execution is Boris-only; explicit legacy-channel operation remains available "
    "only for rollback and forensics. Quiet when settled; emits compact transition, "
    "hold, reconciliation, or failure output. It never creates or mutates Kanban "
    "tasks. Do not schedule additional cron jobs."
)
BORIS_CHANNEL = "C0BPCKPEFCG"
LEGACY_PERMALINK = (
    "https://ylopo.slack.com/archives/C08K8FQKX9S/p2222222222222222"
)
DURABLE_RECOVERY_PATHS = (
    "cron/scripts/just_do_it_blocked_review_requeue_watchdog.py",
    "tests/cron/test_just_do_it_blocked_review_requeue_watchdog.py",
    "cron/scripts/just_do_it_review_reaction_watchdog.py",
    "tests/cron/test_just_do_it_review_reaction_watchdog.py",
    "cron/scripts/boris_intake",
    "tests/cron/boris_intake",
)


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


def _use_canonical_stored_job_identities(fixture: RetirementFixture) -> None:
    schedule_displays = {
        "ee4582d0598f": "every 2m",
        "ff865c33c5ea": "*/2 * * * *",
        RETAIN_ID: "* * * * *",
    }
    for job in fixture.jobs:
        job["script"] = Path(job["script"]).name
        job["schedule"]["display"] = schedule_displays[job["id"]]
    fixture.write_jobs()


def _set_retained_name(fixture: RetirementFixture, name: str) -> None:
    with cron_jobs.use_cron_store(fixture.paths.hermes_home):
        with cron_jobs._jobs_lock():
            jobs = cron_jobs.load_jobs()
            next(job for job in jobs if job["id"] == RETAIN_ID)["name"] = name
            cron_jobs.save_jobs(jobs)


def _expected_legacy_projection_hash(raw: dict) -> str:
    posts = {
        permalink: record
        for permalink, record in raw["posts"].items()
        if f"/{BORIS_CHANNEL}/" not in permalink
    }
    reacted = {}
    for key, record in raw["reacted"].items():
        permalink = key.split(":", 1)[1] if ":" in key else ""
        if f"/{BORIS_CHANNEL}/" not in permalink:
            reacted[key] = record
    encoded = json.dumps(
        {"posts": posts, "reacted": reacted},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _set_legacy_alerted_at(fixture: RetirementFixture, value: object) -> None:
    state = json.loads(fixture.reaction_state_path.read_text())
    state["posts"][LEGACY_PERMALINK]["alerted_at"] = value
    fixture.reaction_state_path.write_text(json.dumps(state, indent=2))


def _prepare_legacy_alert_compensation(
    fixture: RetirementFixture,
    *,
    baseline: int = 111,
    current: int = 222,
) -> Manifest:
    manifest = fixture.complete_retirement()
    _set_legacy_alerted_at(fixture, baseline)
    with cron_jobs.use_cron_store(fixture.paths.hermes_home):
        cron_jobs.pause_job(RETAIN_ID, reason="Boris-only scheduling installation")
    manifest = checkpoint_component(
        fixture.paths,
        manifest,
        "retained-reaction",
        manifest.cutoff_epoch + 200,
    )
    with cron_jobs.use_cron_store(fixture.paths.hermes_home):
        with cron_jobs._jobs_lock():
            jobs = cron_jobs.load_jobs()
            retained = next(job for job in jobs if job["id"] == RETAIN_ID)
            retained["name"] = POST_MIGRATION_RETAINED_NAME
            retained["prompt"] = ALL_CHANNEL_RETAINED_PROMPT
            cron_jobs.save_jobs(jobs)
    _set_legacy_alerted_at(fixture, current)
    return manifest


def _complete_reaction_checkpoint(
    fixture: RetirementFixture, manifest: Manifest, *, offset: int = 200
) -> Manifest:
    with cron_jobs.use_cron_store(fixture.paths.hermes_home):
        cron_jobs.pause_job(RETAIN_ID, reason="installation lock")
    manifest = checkpoint_component(
        fixture.paths,
        manifest,
        "retained-reaction",
        manifest.cutoff_epoch + offset,
    )
    with cron_jobs.use_cron_store(fixture.paths.hermes_home):
        with cron_jobs._jobs_lock():
            jobs = cron_jobs.load_jobs()
            retained = next(job for job in jobs if job["id"] == RETAIN_ID)
            retained.update(
                name=POST_MIGRATION_RETAINED_NAME,
                prompt=BORIS_ONLY_RETAINED_PROMPT,
                enabled=True,
                state="scheduled",
            )
            cron_jobs.save_jobs(jobs)
    fixture.insert_execution(
        RETAIN_ID,
        status="completed",
        claimed_at=fixture.utc_iso(manifest.cutoff_epoch + offset + 1),
    )
    return manifest


def _prepare_checkpoint_manifest(
    fixture: RetirementFixture, component: str
) -> Manifest:
    manifest = fixture.complete_retirement()
    if component == "retained-reaction":
        with cron_jobs.use_cron_store(fixture.paths.hermes_home):
            cron_jobs.pause_job(RETAIN_ID, reason="installation lock")
        return manifest
    return _complete_reaction_checkpoint(fixture, manifest)


def _configure_retained_progress_job(fixture: RetirementFixture) -> None:
    """Give the retained fixture the complete mutable scheduler record shape."""
    workdir = fixture.root / "retained-workdir"
    workdir.mkdir()
    retained = next(job for job in fixture.jobs if job["id"] == RETAIN_ID)
    retained.update(
        {
            "skills": ["review-reactions"],
            "skill": "review-reactions",
            "model": "retained-model",
            "provider": "retained-provider",
            "provider_snapshot": "retained-provider-snapshot",
            "model_snapshot": "retained-model-snapshot",
            "base_url": "https://provider.example/v1",
            "context_from": ["upstream-job"],
            "schedule_display": "* * * * *",
            "repeat": {"times": None, "completed": 7},
            "paused_at": None,
            "paused_reason": None,
            "created_at": fixture.utc_iso(fixture.now - 3600),
            "last_error": None,
            "last_delivery_error": None,
            "deliver": "origin",
            "origin": {"platform": "slack", "channel_id": "C0BPCKPEFCG"},
            "enabled_toolsets": ["slack"],
            "workdir": str(workdir),
            "attach_to_session": False,
        }
    )
    fixture.write_jobs()


def _run_retained_job_three_times(
    fixture: RetirementFixture, monkeypatch: pytest.MonkeyPatch
) -> dict:
    """Exercise real built-in and external recurring scheduler cycles."""
    with cron_jobs.use_cron_store(fixture.paths.hermes_home):
        for index in range(1, 4):
            run_time = datetime.fromtimestamp(
                fixture.now + (index * 60), tz=UTC
            )
            monkeypatch.setattr(
                cron_jobs, "_hermes_now", lambda current=run_time: current
            )
            if index == 2:
                assert cron_jobs.claim_job_for_fire(RETAIN_ID) is True
            else:
                assert cron_jobs.advance_next_run(RETAIN_ID) is True
            cron_jobs.mark_job_run(
                RETAIN_ID,
                success=index != 3,
                error="transient retained failure" if index == 3 else None,
                delivery_error="transient delivery failure" if index == 3 else None,
            )
    return copy.deepcopy(
        next(job for job in fixture.read_jobs() if job["id"] == RETAIN_ID)
    )


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
    incomplete_recovery_ref: str = ""
    recovery_ref: str = ""
    head_commit: str = ""

    def utc_iso(self, epoch: int) -> str:
        return datetime.fromtimestamp(epoch, tz=UTC).isoformat()

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
        return preflight(self.paths, self.now, self.recovery_ref)

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
                            prompt=BORIS_ONLY_RETAINED_PROMPT,
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


def _git(source_root: Path, *args: str, input_text: str | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=source_root,
        check=True,
        text=True,
        input=input_text,
        capture_output=True,
    )
    return result.stdout.strip()


def _git_initialize(source_root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=source_root, check=True)
    _git(source_root, "config", "user.name", "Fixture")
    _git(source_root, "config", "user.email", "fixture@example.test")


def _git_commit(
    source_root: Path, message: str, *, allow_empty: bool = False
) -> str:
    _git(source_root, "add", ".")
    arguments = ["commit", "-qm", message]
    if allow_empty:
        arguments.append("--allow-empty")
    _git(source_root, *arguments)
    return _git(source_root, "rev-parse", "HEAD")


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
            "just-do-it-review-reaction-state-watchdog",
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
    _git_initialize(source_root)
    fixture.incomplete_recovery_ref = _git_commit(
        source_root, "incomplete durable source", allow_empty=True
    )

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
    fixture.recovery_ref = _git_commit(source_root, "complete durable baseline")
    for relative in (
        "cron/scripts/just_do_it_blocked_review_requeue_watchdog.py",
        "tests/cron/test_just_do_it_blocked_review_requeue_watchdog.py",
    ):
        (source_root / relative).unlink()
    fixture.head_commit = _git_commit(source_root, "delete deprecated durable source")

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
        preflight(fixture.paths, fixture.now, fixture.recovery_ref)


def test_preflight_persists_exact_recovery_commit_after_durable_deletion(
    retirement_fixture,
):
    fixture = retirement_fixture
    assert _git(fixture.paths.source_root, "status", "--porcelain") == ""
    assert _git(fixture.paths.source_root, "rev-parse", "HEAD") == fixture.head_commit
    for relative in DURABLE_RECOVERY_PATHS[:2]:
        assert not (fixture.paths.source_root / relative).exists()

    manifest = fixture.preflight_only()
    expected_hashes = {
        relative: _git(
            fixture.paths.source_root,
            "rev-parse",
            f"{fixture.recovery_ref}:{relative}",
        )
        for relative in DURABLE_RECOVERY_PATHS
    }

    assert manifest.repository_commit == fixture.recovery_ref
    assert manifest.repository_tree_hashes == expected_hashes
    restored = Manifest.load(manifest.manifest_path)
    assert restored.repository_commit == fixture.recovery_ref
    assert restored.repository_tree_hashes == expected_hashes


@pytest.mark.parametrize("ref_kind", ("symbolic", "branch", "tag", "short"))
def test_preflight_rejects_non_full_commit_recovery_refs(
    retirement_fixture, ref_kind
):
    fixture = retirement_fixture
    branch = _git(fixture.paths.source_root, "branch", "--show-current")
    _git(fixture.paths.source_root, "tag", "baseline-recovery", fixture.recovery_ref)
    refs = {
        "symbolic": "HEAD",
        "branch": branch,
        "tag": "baseline-recovery",
        "short": fixture.recovery_ref[:12],
    }

    with pytest.raises(RetirementError, match="full 40-hex commit ID"):
        preflight(fixture.paths, fixture.now, refs[ref_kind])


def test_preflight_rejects_full_tag_object_id(retirement_fixture):
    fixture = retirement_fixture
    _git(
        fixture.paths.source_root,
        "tag",
        "-a",
        "annotated-recovery",
        "-m",
        "annotated recovery",
        fixture.recovery_ref,
    )
    tag_object = _git(
        fixture.paths.source_root, "rev-parse", "annotated-recovery"
    )
    assert len(tag_object) == 40

    with pytest.raises(RetirementError, match="exact commit object"):
        preflight(fixture.paths, fixture.now, tag_object)


def test_preflight_rejects_unknown_full_commit_id(retirement_fixture):
    fixture = retirement_fixture

    with pytest.raises(RetirementError, match="exact commit object"):
        preflight(fixture.paths, fixture.now, "0" * 40)


def test_preflight_rejects_recovery_commit_missing_durable_objects(
    retirement_fixture,
):
    fixture = retirement_fixture

    with pytest.raises(RetirementError, match="missing durable recovery path"):
        preflight(
            fixture.paths,
            fixture.now,
            fixture.incomplete_recovery_ref,
        )


def test_preflight_rejects_non_ancestor_recovery_commit(retirement_fixture):
    fixture = retirement_fixture
    tree = _git(
        fixture.paths.source_root,
        "rev-parse",
        f"{fixture.recovery_ref}^{{tree}}",
    )
    non_ancestor = _git(
        fixture.paths.source_root,
        "commit-tree",
        tree,
        input_text="parallel durable baseline\n",
    )

    with pytest.raises(RetirementError, match="not an ancestor of current HEAD"):
        preflight(fixture.paths, fixture.now, non_ancestor)


@pytest.mark.parametrize(
    "relative",
    (
        "cron/scripts/just_do_it_review_reaction_watchdog.py",
        "cron/scripts/boris_intake/daemon.py",
    ),
)
def test_preflight_rejects_retained_durable_source_divergence(
    retirement_fixture, relative
):
    fixture = retirement_fixture
    path = fixture.paths.source_root / relative
    path.write_text(path.read_text() + "retained divergence\n")
    _git_commit(fixture.paths.source_root, "diverge retained durable source")

    with pytest.raises(RetirementError, match="retained durable source diverged"):
        preflight(fixture.paths, fixture.now, fixture.recovery_ref)


@pytest.mark.parametrize(
    "relative",
    (
        "cron/scripts/just_do_it_review_reaction_watchdog.py",
        "cron/scripts/just_do_it_blocked_review_requeue_watchdog.py",
    ),
)
def test_preflight_rejects_uncommitted_durable_changes(
    retirement_fixture, relative
):
    fixture = retirement_fixture
    path = fixture.paths.source_root / relative
    existing = path.read_text() if path.exists() else ""
    path.write_text(existing + "uncommitted durable change\n")

    with pytest.raises(RetirementError, match="uncommitted changes"):
        preflight(fixture.paths, fixture.now, fixture.recovery_ref)


def test_canonical_stored_job_identities_complete_retirement(retirement_fixture):
    fixture = retirement_fixture
    _use_canonical_stored_job_identities(fixture)

    manifest = fixture.preflight_and_pause()
    finalize(fixture.paths, manifest, manifest.cutoff_epoch + 181)
    fixture.insert_execution(
        RETAIN_ID,
        status="completed",
        claimed_at=fixture.utc_iso(manifest.cutoff_epoch + 200),
    )

    assert verify_retired(fixture.paths, manifest, manifest.cutoff_epoch + 240) == []


@pytest.mark.parametrize(
    "script_shape",
    [
        pytest.param("wrong-basename", id="wrong-basename"),
        pytest.param("traversal", id="traversal"),
        pytest.param("nested", id="nested"),
        pytest.param("outside-absolute", id="outside-absolute"),
    ],
)
def test_preflight_rejects_noncanonical_script_identity(
    retirement_fixture, script_shape
):
    fixture = retirement_fixture
    _use_canonical_stored_job_identities(fixture)
    job = next(job for job in fixture.jobs if job["id"] == "ee4582d0598f")
    bad_scripts = {
        "wrong-basename": "unexpected.py",
        "traversal": "../slack_channel_watch.py",
        "nested": "nested/slack_channel_watch.py",
        "outside-absolute": str(fixture.root / "outside" / "slack_channel_watch.py"),
    }
    job["script"] = bad_scripts[script_shape]
    fixture.write_jobs()

    with pytest.raises(RetirementError, match="fingerprint mismatch: script"):
        preflight(fixture.paths, fixture.now, fixture.recovery_ref)


@pytest.mark.parametrize(
    ("job_id", "schedule"),
    [
        pytest.param(
            "ee4582d0598f",
            {"kind": "cron", "expr": "*/2 * * * *", "display": "*/2 * * * *"},
            id="wrong-kind",
        ),
        pytest.param(
            "ee4582d0598f",
            {"kind": "interval", "minutes": 3, "display": "every 3m"},
            id="wrong-minutes",
        ),
        pytest.param(
            "ff865c33c5ea",
            {"kind": "cron", "expr": "* * * * *", "display": "* * * * *"},
            id="wrong-expr",
        ),
        pytest.param(
            "ee4582d0598f",
            {
                "kind": "interval",
                "minutes": 2,
                "display": "every 2m",
                "timezone": "UTC",
            },
            id="unexpected-operational-key",
        ),
    ],
)
def test_preflight_rejects_operational_schedule_drift(
    retirement_fixture, job_id, schedule
):
    fixture = retirement_fixture
    _use_canonical_stored_job_identities(fixture)
    next(job for job in fixture.jobs if job["id"] == job_id)["schedule"] = schedule
    fixture.write_jobs()

    with pytest.raises(RetirementError, match="fingerprint mismatch: schedule"):
        preflight(fixture.paths, fixture.now, fixture.recovery_ref)


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
    manifest = retirement_fixture.complete_retirement()
    with pytest.raises(RetirementError, match="retained job must be paused"):
        checkpoint_component(
            retirement_fixture.paths,
            manifest,
            "retained-reaction",
            retirement_fixture.now,
        )


@pytest.mark.parametrize("component", ("retained-reaction", "boris-intake"))
def test_checkpoint_requires_a_finalized_retirement_manifest(
    retirement_fixture, component
):
    manifest = _prepare_checkpoint_manifest(retirement_fixture, component)
    manifest.phase = "paused"

    with pytest.raises(
        RetirementError, match="checkpoint requires finalized manifest"
    ):
        checkpoint_component(
            retirement_fixture.paths,
            manifest,
            component,
            manifest.cutoff_epoch + 240,
        )


@pytest.mark.parametrize("component", ("retained-reaction", "boris-intake"))
@pytest.mark.parametrize("retired_id", sorted(RETIRE_IDS))
def test_checkpoint_rejects_a_reintroduced_retired_job(
    retirement_fixture, component, retired_id
):
    manifest = _prepare_checkpoint_manifest(retirement_fixture, component)
    jobs = retirement_fixture.read_jobs()
    jobs.append(copy.deepcopy(manifest.jobs[retired_id]))
    retirement_fixture.jobs = jobs
    retirement_fixture.write_jobs()

    with pytest.raises(RetirementError, match="retired jobs remain registered"):
        checkpoint_component(
            retirement_fixture.paths,
            manifest,
            component,
            manifest.cutoff_epoch + 240,
        )


@pytest.mark.parametrize("component", ("retained-reaction", "boris-intake"))
@pytest.mark.parametrize("drift", ("active", "archive"))
def test_checkpoint_rejects_retired_entrypoint_boundary_drift(
    retirement_fixture, component, drift
):
    manifest = _prepare_checkpoint_manifest(retirement_fixture, component)
    fingerprint = manifest.files["retired-channel-script"]
    if drift == "active":
        shutil.copy2(fingerprint.archive_path, fingerprint.active_path)
        expected = "retired entrypoint remains active"
    else:
        fingerprint.archive_path.write_text("tampered retired archive\n")
        expected = "retired archive fingerprint mismatch"

    with pytest.raises(RetirementError, match=expected):
        checkpoint_component(
            retirement_fixture.paths,
            manifest,
            component,
            manifest.cutoff_epoch + 240,
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


def test_retained_name_phase_task8_final_state_accepts_pre_migration_name(
    retirement_fixture,
):
    manifest = retirement_fixture.complete_retirement()
    retirement_fixture.insert_execution(
        RETAIN_ID,
        status="completed",
        claimed_at=retirement_fixture.utc_iso(manifest.cutoff_epoch + 200),
    )
    assert verify_retired(
        retirement_fixture.paths, manifest, manifest.cutoff_epoch + 240
    ) == []


def test_retained_name_phase_task8_rejects_premature_post_migration_name(
    retirement_fixture,
):
    manifest = retirement_fixture.complete_retirement()
    _set_retained_name(retirement_fixture, POST_MIGRATION_RETAINED_NAME)

    errors = verify_retired(
        retirement_fixture.paths, manifest, manifest.cutoff_epoch + 240
    )

    assert errors == ["retained job name is invalid"]


def test_retained_name_phase_post_checkpoint_accepts_post_migration_name(
    retirement_fixture,
):
    manifest = retirement_fixture.complete_retirement()
    manifest = retirement_fixture.checkpoint_and_change_reaction(manifest)

    assert verify_retired(
        retirement_fixture.paths, manifest, manifest.cutoff_epoch + 240
    ) == []


def test_retained_name_phase_post_checkpoint_rejects_pre_migration_name(
    retirement_fixture,
):
    manifest = retirement_fixture.complete_retirement()
    manifest = retirement_fixture.checkpoint_and_change_reaction(manifest)
    _set_retained_name(retirement_fixture, PRE_MIGRATION_RETAINED_NAME)

    errors = verify_retired(
        retirement_fixture.paths, manifest, manifest.cutoff_epoch + 240
    )

    assert errors == ["retained job name is invalid"]


@pytest.mark.parametrize(
    ("retained_name", "accepted"),
    (
        pytest.param(PRE_MIGRATION_RETAINED_NAME, False, id="pre-migration"),
        pytest.param(POST_MIGRATION_RETAINED_NAME, True, id="post-migration"),
    ),
)
def test_retained_name_phase_boris_checkpoint_requires_post_migration_name(
    retirement_fixture, retained_name, accepted
):
    manifest = retirement_fixture.complete_retirement()
    manifest = retirement_fixture.checkpoint_and_change_reaction(manifest)
    _set_retained_name(retirement_fixture, retained_name)

    if not accepted:
        with pytest.raises(
            RetirementError,
            match="retained watchdog must be enabled and healthy before Boris checkpoint",
        ):
            checkpoint_component(
                retirement_fixture.paths,
                manifest,
                "boris-intake",
                manifest.cutoff_epoch + 210,
            )
        return

    checkpointed = checkpoint_component(
        retirement_fixture.paths,
        manifest,
        "boris-intake",
        manifest.cutoff_epoch + 210,
    )
    assert len(checkpointed.component_checkpoints["boris-intake"]) == 1


@pytest.mark.parametrize("phase", ("pre-migration", "post-migration"))
def test_retained_name_phase_rejects_arbitrary_name_in_every_phase(
    retirement_fixture, phase
):
    manifest = retirement_fixture.complete_retirement()
    if phase == "post-migration":
        manifest = retirement_fixture.checkpoint_and_change_reaction(manifest)
    _set_retained_name(retirement_fixture, "unapproved-review-reaction-watchdog")

    errors = verify_retired(
        retirement_fixture.paths, manifest, manifest.cutoff_epoch + 240
    )

    assert errors == ["retained job name is invalid"]


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


@pytest.mark.parametrize("duplicate_id", (*sorted(RETIRE_IDS), RETAIN_ID))
@pytest.mark.parametrize("apply", (False, True), ids=("dry-run", "apply"))
def test_finalize_rejects_duplicate_protected_job_records_before_mutation(
    retirement_fixture, capsys, duplicate_id, apply
):
    manifest = retirement_fixture.preflight_and_pause()
    jobs = retirement_fixture.read_jobs()
    jobs.append(copy.deepcopy(next(job for job in jobs if job["id"] == duplicate_id)))
    retirement_fixture.jobs = jobs
    retirement_fixture.write_jobs()
    jobs_before = retirement_fixture.paths.jobs_path.read_bytes()
    manifest_before = manifest.manifest_path.read_bytes()
    command = [
        "finalize",
        "--manifest",
        str(manifest.manifest_path),
        "--now-epoch",
        str(manifest.cutoff_epoch + 181),
    ]
    if apply:
        command.append("--apply")

    result = controller_main(command)

    assert result == 2
    assert "expected exactly one" in capsys.readouterr().out
    assert retirement_fixture.paths.jobs_path.read_bytes() == jobs_before
    assert manifest.manifest_path.read_bytes() == manifest_before
    assert all(path.exists() for path in retirement_fixture.retired_live_paths())
    assert retirement_fixture.output_paths_are_present_for(RETIRE_IDS)


def test_finalize_dry_run_accepts_multiple_retained_scheduler_runs(
    retirement_fixture, monkeypatch, capsys
):
    _configure_retained_progress_job(retirement_fixture)
    manifest = retirement_fixture.preflight_and_pause()
    latest_retained = _run_retained_job_three_times(retirement_fixture, monkeypatch)
    preflight_retained = manifest.jobs[RETAIN_ID]
    assert latest_retained["repeat"]["completed"] == 10
    for field in (
        "last_run_at",
        "next_run_at",
        "last_status",
        "last_error",
        "last_delivery_error",
    ):
        assert latest_retained[field] != preflight_retained.get(field)
    assert latest_retained["fire_claim"] is None
    assert latest_retained["enabled"] is True
    assert latest_retained["state"] == "scheduled"
    jobs_before = retirement_fixture.paths.jobs_path.read_bytes()
    manifest_before = manifest.manifest_path.read_bytes()

    result = controller_main(
        [
            "finalize",
            "--manifest",
            str(manifest.manifest_path),
            "--now-epoch",
            str(manifest.cutoff_epoch + 181),
        ]
    )

    assert result == 0
    assert '"apply": false' in capsys.readouterr().out
    assert retirement_fixture.paths.jobs_path.read_bytes() == jobs_before
    assert manifest.manifest_path.read_bytes() == manifest_before
    assert next(
        job for job in retirement_fixture.read_jobs() if job["id"] == RETAIN_ID
    ) == latest_retained


def test_finalize_apply_accepts_multiple_retained_scheduler_runs(
    retirement_fixture, monkeypatch
):
    _configure_retained_progress_job(retirement_fixture)
    manifest = retirement_fixture.preflight_and_pause()
    latest_retained = _run_retained_job_three_times(retirement_fixture, monkeypatch)

    result = controller_main(
        [
            "finalize",
            "--manifest",
            str(manifest.manifest_path),
            "--now-epoch",
            str(manifest.cutoff_epoch + 181),
            "--apply",
        ]
    )

    assert result == 0
    assert Manifest.load(manifest.manifest_path).phase == "finalized"
    assert retirement_fixture.read_jobs() == [latest_retained]


def test_finalize_dry_run_accepts_an_exact_external_fire_claim_transition(
    retirement_fixture, monkeypatch
):
    _configure_retained_progress_job(retirement_fixture)
    manifest = retirement_fixture.preflight_and_pause()
    claim_time = datetime.fromtimestamp(
        manifest.cutoff_epoch + 120, tz=UTC
    )
    monkeypatch.setattr(cron_jobs, "_hermes_now", lambda: claim_time)
    monkeypatch.setenv("HERMES_MACHINE_ID", "fixture-scheduler:123")
    with cron_jobs.use_cron_store(retirement_fixture.paths.hermes_home):
        assert cron_jobs.claim_job_for_fire(RETAIN_ID) is True

    assert controller_main(
        [
            "finalize",
            "--manifest",
            str(manifest.manifest_path),
            "--now-epoch",
            str(manifest.cutoff_epoch + 181),
        ]
    ) == 0


def test_finalize_observation_window_rejects_completion_before_preflight(
    retirement_fixture,
):
    _configure_retained_progress_job(retirement_fixture)
    manifest = retirement_fixture.preflight_and_pause()
    jobs = retirement_fixture.read_jobs()
    retained = next(job for job in jobs if job["id"] == RETAIN_ID)
    retained.update(
        last_run_at=retirement_fixture.utc_iso(manifest.created_epoch - 1),
        next_run_at=retirement_fixture.utc_iso(manifest.created_epoch),
        last_status="ok",
        last_error=None,
        last_delivery_error=None,
        fire_claim=None,
    )
    retained["repeat"]["completed"] += 1
    retirement_fixture.jobs = jobs
    retirement_fixture.write_jobs()

    with pytest.raises(RetirementError, match="new completion predates preflight"):
        finalize(
            retirement_fixture.paths, manifest, manifest.cutoff_epoch + 181
        )


def test_finalize_observation_window_rejects_completion_after_finalize_now(
    retirement_fixture,
):
    _configure_retained_progress_job(retirement_fixture)
    manifest = retirement_fixture.preflight_and_pause()
    finalize_epoch = manifest.cutoff_epoch + 181
    jobs = retirement_fixture.read_jobs()
    retained = next(job for job in jobs if job["id"] == RETAIN_ID)
    retained.update(
        last_run_at=retirement_fixture.utc_iso(finalize_epoch + 1),
        next_run_at=retirement_fixture.utc_iso(manifest.created_epoch + 240),
        last_status="ok",
        last_error=None,
        last_delivery_error=None,
        fire_claim=None,
    )
    retained["repeat"]["completed"] += 1
    retirement_fixture.jobs = jobs
    retirement_fixture.write_jobs()

    with pytest.raises(
        RetirementError, match="new completion exceeds finalize observation"
    ):
        finalize(retirement_fixture.paths, manifest, finalize_epoch)


def test_finalize_observation_window_rejects_unchanged_claim_before_preflight(
    retirement_fixture,
):
    _configure_retained_progress_job(retirement_fixture)
    retained = next(
        job for job in retirement_fixture.jobs if job["id"] == RETAIN_ID
    )
    retained["fire_claim"] = {
        "at": retirement_fixture.utc_iso(retirement_fixture.now - 60),
        "by": "fixture-scheduler:123",
    }
    retained["next_run_at"] = retirement_fixture.utc_iso(retirement_fixture.now)
    retirement_fixture.write_jobs()
    manifest = retirement_fixture.preflight_and_pause()

    with pytest.raises(
        RetirementError, match="current fire_claim predates preflight"
    ):
        finalize(
            retirement_fixture.paths, manifest, manifest.cutoff_epoch + 181
        )


def test_finalize_dry_run_observation_window_rejects_claim_after_finalize_now(
    retirement_fixture, capsys
):
    _configure_retained_progress_job(retirement_fixture)
    manifest = retirement_fixture.preflight_and_pause()
    finalize_epoch = manifest.cutoff_epoch + 181
    jobs = retirement_fixture.read_jobs()
    retained = next(job for job in jobs if job["id"] == RETAIN_ID)
    retained["fire_claim"] = {
        "at": retirement_fixture.utc_iso(finalize_epoch + 1),
        "by": "fixture-scheduler:123",
    }
    retained["next_run_at"] = retirement_fixture.utc_iso(
        manifest.created_epoch + 240
    )
    retirement_fixture.jobs = jobs
    retirement_fixture.write_jobs()

    result = controller_main(
        [
            "finalize",
            "--manifest",
            str(manifest.manifest_path),
            "--now-epoch",
            str(finalize_epoch),
        ]
    )

    assert result == 2
    assert "current fire_claim exceeds finalize observation" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("delivery_error", "completed", "expected_error"),
    [
        pytest.param(
            "orphaned delivery failure",
            0,
            "last_delivery_error is not coupled to a completion",
            id="delivery-error-without-completion",
        ),
        pytest.param(
            None,
            1,
            "repeat.completed is not coupled to a completion",
            id="completed-count-without-completion",
        ),
    ],
)
def test_finalize_observation_window_rejects_no_completion_scheduler_state(
    retirement_fixture, delivery_error, completed, expected_error
):
    _configure_retained_progress_job(retirement_fixture)
    retained = next(
        job for job in retirement_fixture.jobs if job["id"] == RETAIN_ID
    )
    retained.update(
        last_run_at=None,
        last_status=None,
        last_error=None,
        last_delivery_error=delivery_error,
    )
    retained["repeat"]["completed"] = completed
    retirement_fixture.write_jobs()
    manifest = retirement_fixture.preflight_and_pause()

    with pytest.raises(RetirementError, match=expected_error):
        finalize(
            retirement_fixture.paths, manifest, manifest.cutoff_epoch + 181
        )


def test_finalize_observation_window_rejects_completion_with_unchanged_claim(
    retirement_fixture,
):
    _configure_retained_progress_job(retirement_fixture)
    retained = next(
        job for job in retirement_fixture.jobs if job["id"] == RETAIN_ID
    )
    retained["fire_claim"] = {
        "at": retirement_fixture.utc_iso(retirement_fixture.now + 120),
        "by": "fixture-scheduler:123",
    }
    retained["next_run_at"] = retirement_fixture.utc_iso(
        retirement_fixture.now + 180
    )
    retirement_fixture.write_jobs()
    manifest = retirement_fixture.preflight_and_pause()
    jobs = retirement_fixture.read_jobs()
    retained = next(job for job in jobs if job["id"] == RETAIN_ID)
    retained.update(
        last_run_at=retirement_fixture.utc_iso(manifest.created_epoch + 60),
        last_status="ok",
        last_error=None,
        last_delivery_error=None,
    )
    retained["repeat"]["completed"] += 1
    retirement_fixture.jobs = jobs
    retirement_fixture.write_jobs()

    with pytest.raises(RetirementError, match="new completion did not clear fire_claim"):
        finalize(
            retirement_fixture.paths, manifest, manifest.cutoff_epoch + 181
        )


def test_finalize_observation_window_rejects_finalize_now_before_preflight(
    retirement_fixture,
):
    _configure_retained_progress_job(retirement_fixture)
    manifest = retirement_fixture.preflight_and_pause()
    manifest.cutoff_epoch = manifest.created_epoch - 1000
    manifest.cutoff_iso = retirement_fixture.utc_iso(manifest.cutoff_epoch)

    with pytest.raises(
        RetirementError, match="finalize observation time predates preflight"
    ):
        finalize(
            retirement_fixture.paths, manifest, manifest.created_epoch - 1
        )


@pytest.mark.parametrize(
    ("transition", "offset"),
    [
        pytest.param("completion", 0, id="completion-at-lower-bound"),
        pytest.param("completion", 182, id="completion-at-upper-bound"),
        pytest.param("claim", 0, id="claim-at-lower-bound"),
        pytest.param("claim", 182, id="claim-at-upper-bound"),
    ],
)
def test_finalize_observation_window_accepts_inclusive_scheduler_boundaries(
    retirement_fixture, monkeypatch, transition, offset
):
    _configure_retained_progress_job(retirement_fixture)
    manifest = retirement_fixture.preflight_and_pause()
    finalize_epoch = manifest.created_epoch + 182
    transition_time = datetime.fromtimestamp(
        manifest.created_epoch + offset, tz=UTC
    )
    monkeypatch.setattr(cron_jobs, "_hermes_now", lambda: transition_time)
    monkeypatch.setenv("HERMES_MACHINE_ID", "fixture-scheduler:123")
    with cron_jobs.use_cron_store(retirement_fixture.paths.hermes_home):
        if transition == "completion":
            assert cron_jobs.advance_next_run(RETAIN_ID) is True
            cron_jobs.mark_job_run(RETAIN_ID, success=True)
        else:
            assert cron_jobs.claim_job_for_fire(RETAIN_ID) is True
    latest_retained = next(
        job for job in retirement_fixture.read_jobs() if job["id"] == RETAIN_ID
    )

    finalized = finalize(retirement_fixture.paths, manifest, finalize_epoch)

    assert finalized.phase == "finalized"
    assert retirement_fixture.read_jobs() == [latest_retained]


def test_finalize_observation_window_accepts_successful_delivery_failure_completion(
    retirement_fixture, monkeypatch
):
    _configure_retained_progress_job(retirement_fixture)
    manifest = retirement_fixture.preflight_and_pause()
    run_time = datetime.fromtimestamp(
        manifest.created_epoch + 60, tz=UTC
    )
    monkeypatch.setattr(cron_jobs, "_hermes_now", lambda: run_time)
    monkeypatch.setenv("HERMES_MACHINE_ID", "fixture-scheduler:123")
    with cron_jobs.use_cron_store(retirement_fixture.paths.hermes_home):
        assert cron_jobs.claim_job_for_fire(RETAIN_ID) is True
        cron_jobs.mark_job_run(
            RETAIN_ID,
            success=True,
            delivery_error="fixture delivery failed",
        )
    latest_retained = next(
        job for job in retirement_fixture.read_jobs() if job["id"] == RETAIN_ID
    )
    # upstream (Aug 2026): agent success + delivery failure is recorded as
    # ``delivery_failed`` (never ``ok``) and still counts as a completion.
    assert latest_retained["last_status"] == "delivery_failed"
    assert latest_retained["last_error"] is None
    assert latest_retained["last_delivery_error"] == "fixture delivery failed"
    assert latest_retained["fire_claim"] is None

    finalize(
        retirement_fixture.paths, manifest, manifest.cutoff_epoch + 181
    )

    assert retirement_fixture.read_jobs() == [latest_retained]


def test_finalize_observation_window_accepts_absent_zero_completion_counter(
    retirement_fixture,
):
    _configure_retained_progress_job(retirement_fixture)
    retained = next(
        job for job in retirement_fixture.jobs if job["id"] == RETAIN_ID
    )
    retained.update(
        last_run_at=None,
        last_status=None,
        last_error=None,
        last_delivery_error=None,
    )
    retained["repeat"].pop("completed")
    retirement_fixture.write_jobs()
    manifest = retirement_fixture.preflight_and_pause()
    latest_retained = next(
        job for job in retirement_fixture.read_jobs() if job["id"] == RETAIN_ID
    )

    finalize(
        retirement_fixture.paths, manifest, manifest.cutoff_epoch + 181
    )

    assert retirement_fixture.read_jobs() == [latest_retained]


def test_finalize_observation_window_accepts_absent_claim_after_completion(
    retirement_fixture,
):
    _configure_retained_progress_job(retirement_fixture)
    manifest = retirement_fixture.preflight_and_pause()
    jobs = retirement_fixture.read_jobs()
    retained = next(job for job in jobs if job["id"] == RETAIN_ID)
    assert "fire_claim" not in retained
    retained.update(
        last_run_at=retirement_fixture.utc_iso(manifest.created_epoch + 60),
        next_run_at=retirement_fixture.utc_iso(manifest.created_epoch + 120),
        last_status="ok",
        last_error=None,
        last_delivery_error=None,
    )
    retained["repeat"]["completed"] += 1
    retirement_fixture.jobs = jobs
    retirement_fixture.write_jobs()
    latest_retained = copy.deepcopy(retained)

    finalize(
        retirement_fixture.paths, manifest, manifest.cutoff_epoch + 181
    )

    assert retirement_fixture.read_jobs() == [latest_retained]


def test_finalize_rejects_a_non_null_run_claim_on_the_retained_cron(
    retirement_fixture,
):
    _configure_retained_progress_job(retirement_fixture)
    manifest = retirement_fixture.preflight_and_pause()
    jobs = retirement_fixture.read_jobs()
    retained = next(job for job in jobs if job["id"] == RETAIN_ID)
    retained["run_claim"] = {
        "at": retirement_fixture.utc_iso(manifest.cutoff_epoch + 120),
        "by": "fixture-scheduler:123",
    }
    retirement_fixture.jobs = jobs
    retirement_fixture.write_jobs()

    with pytest.raises(RetirementError, match="retained job"):
        finalize(
            retirement_fixture.paths, manifest, manifest.cutoff_epoch + 181
        )


@pytest.mark.parametrize(
    ("claim_at", "next_run_at"),
    [
        pytest.param(
            "2026-08-08T19:58:00+00:00",
            "2026-08-08T20:00:00+00:00",
            id="claim-before-latest-completion",
        ),
        pytest.param(
            "2026-08-08T19:59:00+00:00",
            "2026-08-08T20:00:00+00:00",
            id="claim-not-after-latest-completion",
        ),
        pytest.param(
            "2026-08-08T20:01:00+00:00",
            "2026-08-08T20:00:00+00:00",
            id="claim-after-next-run",
        ),
        pytest.param(
            "2026-08-08T21:02:00+00:00",
            "2026-08-08T21:05:00+00:00",
            id="next-run-not-coupled-to-claim",
        ),
    ],
)
def test_finalize_rejects_unordered_or_uncoupled_retained_fire_claims(
    retirement_fixture, claim_at, next_run_at
):
    _configure_retained_progress_job(retirement_fixture)
    manifest = retirement_fixture.preflight_and_pause()
    jobs = retirement_fixture.read_jobs()
    retained = next(job for job in jobs if job["id"] == RETAIN_ID)
    retained["fire_claim"] = {
        "at": claim_at,
        "by": "fixture-scheduler:123",
    }
    retained["next_run_at"] = next_run_at
    retirement_fixture.jobs = jobs
    retirement_fixture.write_jobs()

    with pytest.raises(RetirementError, match="retained job"):
        finalize(
            retirement_fixture.paths, manifest, manifest.cutoff_epoch + 181
        )


def test_finalize_rejects_ok_completion_with_a_last_error(retirement_fixture):
    _configure_retained_progress_job(retirement_fixture)
    manifest = retirement_fixture.preflight_and_pause()
    jobs = retirement_fixture.read_jobs()
    retained = next(job for job in jobs if job["id"] == RETAIN_ID)
    retained.update(
        last_run_at=retirement_fixture.utc_iso(manifest.cutoff_epoch + 59),
        next_run_at=retirement_fixture.utc_iso(manifest.cutoff_epoch + 119),
        last_status="ok",
        last_error="impossible stale failure",
        last_delivery_error=None,
        fire_claim=None,
    )
    retained["repeat"]["completed"] += 1
    retirement_fixture.jobs = jobs
    retirement_fixture.write_jobs()

    with pytest.raises(RetirementError, match="retained job"):
        finalize(
            retirement_fixture.paths, manifest, manifest.cutoff_epoch + 181
        )


def test_finalize_rejects_a_completion_count_without_a_new_last_run(
    retirement_fixture,
):
    _configure_retained_progress_job(retirement_fixture)
    manifest = retirement_fixture.preflight_and_pause()
    jobs = retirement_fixture.read_jobs()
    retained = next(job for job in jobs if job["id"] == RETAIN_ID)
    retained["repeat"]["completed"] += 1
    retirement_fixture.jobs = jobs
    retirement_fixture.write_jobs()

    with pytest.raises(RetirementError, match="retained job"):
        finalize(
            retirement_fixture.paths, manifest, manifest.cutoff_epoch + 181
        )


def test_finalize_rejects_a_new_last_run_without_a_completion_count(
    retirement_fixture,
):
    _configure_retained_progress_job(retirement_fixture)
    manifest = retirement_fixture.preflight_and_pause()
    jobs = retirement_fixture.read_jobs()
    retained = next(job for job in jobs if job["id"] == RETAIN_ID)
    retained.update(
        last_run_at=retirement_fixture.utc_iso(manifest.cutoff_epoch + 59),
        next_run_at=retirement_fixture.utc_iso(manifest.cutoff_epoch + 119),
        last_status="error",
        last_error="retained failure",
        last_delivery_error=None,
        fire_claim=None,
    )
    retirement_fixture.jobs = jobs
    retirement_fixture.write_jobs()

    with pytest.raises(RetirementError, match="retained job"):
        finalize(
            retirement_fixture.paths, manifest, manifest.cutoff_epoch + 181
        )


def test_finalize_rejects_a_last_run_not_before_the_next_run(retirement_fixture):
    _configure_retained_progress_job(retirement_fixture)
    manifest = retirement_fixture.preflight_and_pause()
    jobs = retirement_fixture.read_jobs()
    retained = next(job for job in jobs if job["id"] == RETAIN_ID)
    same_time = retirement_fixture.utc_iso(manifest.cutoff_epoch + 119)
    retained.update(
        last_run_at=same_time,
        next_run_at=same_time,
        last_status="ok",
        last_error=None,
        last_delivery_error=None,
        fire_claim=None,
    )
    retained["repeat"]["completed"] += 1
    retirement_fixture.jobs = jobs
    retirement_fixture.write_jobs()

    with pytest.raises(RetirementError, match="retained job"):
        finalize(
            retirement_fixture.paths, manifest, manifest.cutoff_epoch + 181
        )


@pytest.mark.parametrize(
    "runtime_changes",
    [
        pytest.param(
            {"last_status": "error", "last_error": "retained failure"},
            id="status-and-error",
        ),
        pytest.param(
            {"last_delivery_error": "delivery failure"},
            id="delivery-error",
        ),
    ],
)
def test_finalize_rejects_completion_fields_changed_without_a_new_last_run(
    retirement_fixture, runtime_changes
):
    _configure_retained_progress_job(retirement_fixture)
    manifest = retirement_fixture.preflight_and_pause()
    jobs = retirement_fixture.read_jobs()
    retained = next(job for job in jobs if job["id"] == RETAIN_ID)
    retained.update(runtime_changes)
    retirement_fixture.jobs = jobs
    retirement_fixture.write_jobs()

    with pytest.raises(RetirementError, match="retained job"):
        finalize(
            retirement_fixture.paths, manifest, manifest.cutoff_epoch + 181
        )


@pytest.mark.parametrize(
    ("field", "drifted_value"),
    [
        pytest.param("id", "different-id", id="id"),
        pytest.param("name", "drifted retained name", id="name"),
        pytest.param("prompt", "drifted retained prompt", id="prompt"),
        pytest.param("script", "unexpected.py", id="script"),
        pytest.param(
            "script",
            "just_do_it_review_reaction_watchdog.py",
            id="script-storage-representation",
        ),
        pytest.param(
            "schedule",
            {"kind": "cron", "expr": "*/5 * * * *"},
            id="schedule-operational",
        ),
        pytest.param(
            "schedule",
            {"kind": "cron", "expr": "* * * * *", "display": "* * * * *"},
            id="schedule-derived-display-representation",
        ),
        pytest.param("no_agent", False, id="no-agent"),
        pytest.param("deliver", "local", id="delivery"),
        pytest.param(
            "origin", {"platform": "telegram", "chat_id": "other"}, id="origin"
        ),
        pytest.param("provider", "other-provider", id="provider"),
        pytest.param("provider_snapshot", "other-snapshot", id="provider-snapshot"),
        pytest.param("model", "other-model", id="model"),
        pytest.param("model_snapshot", "other-snapshot", id="model-snapshot"),
        pytest.param("base_url", "https://other.example/v1", id="base-url"),
        pytest.param("workdir", "/different/workdir", id="workdir"),
        pytest.param("enabled_toolsets", ["terminal"], id="toolsets"),
        pytest.param("skills", ["other-skill"], id="skills"),
        pytest.param("skill", "other-skill", id="legacy-skill"),
        pytest.param("context_from", ["other-upstream"], id="context-from"),
        pytest.param("attach_to_session", True, id="delivery-attachment"),
        pytest.param("repeat.times", 5, id="repeat-limit"),
        pytest.param("opaque_future_config", {"changed": True}, id="unknown-config"),
    ],
)
def test_finalize_rejects_retained_immutable_or_config_drift(
    retirement_fixture, field, drifted_value
):
    _configure_retained_progress_job(retirement_fixture)
    manifest = retirement_fixture.preflight_and_pause()
    jobs = retirement_fixture.read_jobs()
    retained = next(job for job in jobs if job["id"] == RETAIN_ID)
    if field == "repeat.times":
        retained["repeat"]["times"] = drifted_value
    else:
        retained[field] = drifted_value
    retirement_fixture.jobs = jobs
    retirement_fixture.write_jobs()

    with pytest.raises(RetirementError, match="retained job"):
        finalize(
            retirement_fixture.paths, manifest, manifest.cutoff_epoch + 181
        )

    assert all(path.exists() for path in retirement_fixture.retired_live_paths())
    assert retirement_fixture.output_paths_are_present_for(RETIRE_IDS)


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        pytest.param("last_run_at", 123, id="last-run-type"),
        pytest.param(
            "last_run_at", "2026-08-08T19:58:00+00:00", id="last-run-regression"
        ),
        pytest.param(
            "last_run_at", "2026-08-08T20:01:00", id="last-run-naive"
        ),
        pytest.param("next_run_at", None, id="next-run-type"),
        pytest.param(
            "next_run_at", "2026-08-08T19:59:30+00:00", id="next-run-regression"
        ),
        pytest.param("next_run_at", "not-a-timestamp", id="next-run-shape"),
        pytest.param("last_status", "completed", id="last-status-value"),
        pytest.param("last_status", None, id="last-status-regression"),
        pytest.param("last_error", ["not", "text"], id="last-error-type"),
        pytest.param("last_delivery_error", 7, id="delivery-error-type"),
        pytest.param(
            "fire_claim",
            {"at": "2026-08-08T20:01:00+00:00"},
            id="fire-claim-missing-owner",
        ),
        pytest.param(
            "fire_claim",
            {
                "at": "2026-08-08T20:01:00+00:00",
                "by": "owner",
                "unexpected": True,
            },
            id="fire-claim-extra-field",
        ),
        pytest.param(
            "run_claim", {"at": "not-a-timestamp", "by": "owner"}, id="run-claim-time"
        ),
        pytest.param("repeat.completed", 6, id="repeat-completed-regression"),
        pytest.param("repeat.completed", True, id="repeat-completed-bool"),
        pytest.param("repeat.completed", -1, id="repeat-completed-negative"),
    ],
)
def test_finalize_rejects_invalid_retained_runtime_progress(
    retirement_fixture, field, invalid_value
):
    _configure_retained_progress_job(retirement_fixture)
    manifest = retirement_fixture.preflight_and_pause()
    jobs = retirement_fixture.read_jobs()
    retained = next(job for job in jobs if job["id"] == RETAIN_ID)
    if field == "repeat.completed":
        retained["repeat"]["completed"] = invalid_value
    else:
        retained[field] = invalid_value
    retirement_fixture.jobs = jobs
    retirement_fixture.write_jobs()

    with pytest.raises(RetirementError, match="retained job"):
        finalize(
            retirement_fixture.paths, manifest, manifest.cutoff_epoch + 181
        )


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        pytest.param("enabled", False, id="disabled"),
        pytest.param("state", "paused", id="paused"),
        pytest.param("state", "error", id="error-state"),
    ],
)
def test_finalize_rejects_disabled_or_non_scheduled_retained_job(
    retirement_fixture, field, invalid_value
):
    _configure_retained_progress_job(retirement_fixture)
    manifest = retirement_fixture.preflight_and_pause()
    jobs = retirement_fixture.read_jobs()
    next(job for job in jobs if job["id"] == RETAIN_ID)[field] = invalid_value
    retirement_fixture.jobs = jobs
    retirement_fixture.write_jobs()

    with pytest.raises(RetirementError, match="retained job"):
        finalize(
            retirement_fixture.paths, manifest, manifest.cutoff_epoch + 181
        )


def test_finalize_compensation_preserves_latest_retained_scheduler_progress(
    retirement_fixture, monkeypatch
):
    _configure_retained_progress_job(retirement_fixture)
    manifest = retirement_fixture.preflight_and_pause()
    captured_retained = _run_retained_job_three_times(
        retirement_fixture, monkeypatch
    )
    paused_retiring = {
        job["id"]: copy.deepcopy(job)
        for job in retirement_fixture.read_jobs()
        if job["id"] in RETIRE_IDS
    }
    real_remove = cron_jobs.remove_job
    real_save = cron_jobs.save_jobs
    registry_writes: list[list[dict]] = []
    interleaved_retained: dict = {}

    def observe_save(jobs, **kwargs):
        registry_writes.append(copy.deepcopy(jobs))
        return real_save(jobs, **kwargs)

    def fail_second_removal(job_id):
        if job_id == "ff865c33c5ea":
            run_time = datetime.fromtimestamp(
                retirement_fixture.now + 240, tz=UTC
            )
            monkeypatch.setattr(
                cron_jobs, "_hermes_now", lambda: run_time
            )
            assert cron_jobs.advance_next_run(RETAIN_ID) is True
            cron_jobs.mark_job_run(RETAIN_ID, success=True)
            interleaved_retained.update(
                copy.deepcopy(
                    next(
                        job
                        for job in cron_jobs.load_jobs()
                        if job["id"] == RETAIN_ID
                    )
                )
            )
            return False
        return real_remove(job_id)

    monkeypatch.setattr(cron_jobs, "save_jobs", observe_save)
    monkeypatch.setattr(cron_jobs, "remove_job", fail_second_removal)
    with pytest.raises(RetirementError, match="could not be removed"):
        finalize(
            retirement_fixture.paths, manifest, manifest.cutoff_epoch + 181
        )

    assert interleaved_retained != captured_retained
    restored = {job["id"]: job for job in retirement_fixture.read_jobs()}
    assert restored[RETAIN_ID] == interleaved_retained
    assert {job_id: restored[job_id] for job_id in RETIRE_IDS} == paused_retiring
    assert registry_writes
    assert all(
        job.get("enabled") is False and job.get("state") == "paused"
        for written in registry_writes
        for job in written
        if job.get("id") in RETIRE_IDS
    )
    final_write = {
        job["id"]: job
        for job in registry_writes[-1]
        if job.get("id") in RETIRE_IDS
    }
    assert final_write == paused_retiring
    assert all(path.exists() for path in retirement_fixture.retired_live_paths())
    assert retirement_fixture.output_paths_are_present_for(RETIRE_IDS)


def test_finalize_compensation_restores_verified_sources_before_retired_records(
    retirement_fixture, monkeypatch
):
    manifest = retirement_fixture.preflight_and_pause()
    entrypoints = tuple(
        copy.deepcopy(manifest.files[key])
        for key in retirement_controller.ENTRYPOINTS
    )
    real_remove = cron_jobs.remove_job
    real_save = cron_jobs.save_jobs
    source_state_at_registry_restore: list[bool] = []

    def observe_save(jobs, **kwargs):
        if RETIRE_IDS <= {job.get("id") for job in jobs}:
            source_state_at_registry_restore.append(
                all(
                    fingerprint.active_path.is_file()
                    and _hash_file(fingerprint.active_path) == fingerprint.sha256
                    for fingerprint in entrypoints
                )
            )
        return real_save(jobs, **kwargs)

    def fail_second_removal(job_id):
        if job_id == "ff865c33c5ea":
            return False
        return real_remove(job_id)

    monkeypatch.setattr(cron_jobs, "save_jobs", observe_save)
    monkeypatch.setattr(cron_jobs, "remove_job", fail_second_removal)

    with pytest.raises(RetirementError, match="could not be removed"):
        finalize(
            retirement_fixture.paths, manifest, manifest.cutoff_epoch + 181
        )

    assert source_state_at_registry_restore == [True]


def test_finalize_compensation_source_verification_failure_gates_registry_restore(
    retirement_fixture, monkeypatch
):
    manifest = retirement_fixture.preflight_and_pause()
    target = manifest.files["retired-channel-script"].active_path
    real_remove = cron_jobs.remove_job
    real_require = retirement_controller._require_match
    real_save = cron_jobs.save_jobs
    removal_failed = False
    verification_failed = False
    registry_restores = 0

    def fail_second_removal(job_id):
        nonlocal removal_failed
        if job_id == "ff865c33c5ea":
            removal_failed = True
            return False
        return real_remove(job_id)

    def fail_source_verification(fingerprint, path=None):
        nonlocal verification_failed
        checked_path = path or fingerprint.active_path
        if removal_failed and not verification_failed and checked_path == target:
            verification_failed = True
            raise RetirementError("injected source compensation verification failure")
        return real_require(fingerprint, path)

    def observe_save(jobs, **kwargs):
        nonlocal registry_restores
        if RETIRE_IDS <= {job.get("id") for job in jobs}:
            registry_restores += 1
        return real_save(jobs, **kwargs)

    monkeypatch.setattr(cron_jobs, "remove_job", fail_second_removal)
    monkeypatch.setattr(
        retirement_controller, "_require_match", fail_source_verification
    )
    monkeypatch.setattr(cron_jobs, "save_jobs", observe_save)

    with pytest.raises(
        RetirementError, match="exact paused-state compensation failed"
    ):
        finalize(
            retirement_fixture.paths, manifest, manifest.cutoff_epoch + 181
        )

    restored_ids = {job["id"] for job in retirement_fixture.read_jobs()}
    assert verification_failed is True
    assert registry_restores == 0
    assert RETIRE_IDS - restored_ids
    assert Manifest.load(manifest.manifest_path).phase == "paused"


def test_finalize_compensation_tracks_entrypoint_before_move_reports_failure(
    retirement_fixture, monkeypatch
):
    manifest = retirement_fixture.preflight_and_pause()
    original = copy.deepcopy(manifest.files["retired-channel-script"])
    archived = (
        manifest.archive_dir
        / "retired-entrypoints"
        / original.active_path.name
    )
    real_replace = os.replace
    move_reported_failure = False

    def fail_after_entrypoint_move(source, destination):
        nonlocal move_reported_failure
        result = real_replace(source, destination)
        if (
            not move_reported_failure
            and source == original.active_path
            and destination == archived
        ):
            move_reported_failure = True
            raise OSError("injected failure after entrypoint move")
        return result

    monkeypatch.setattr(
        retirement_controller.os, "replace", fail_after_entrypoint_move
    )

    with pytest.raises(OSError, match="failure after entrypoint move"):
        finalize(
            retirement_fixture.paths, manifest, manifest.cutoff_epoch + 181
        )

    restored_ids = {job["id"] for job in retirement_fixture.read_jobs()}
    assert move_reported_failure is True
    assert original.active_path.is_file()
    assert _hash_file(original.active_path) == original.sha256
    assert not archived.exists()
    assert RETIRE_IDS <= restored_ids


def test_finalize_compensation_active_archive_collision_gates_registry_restore(
    retirement_fixture, monkeypatch
):
    manifest = retirement_fixture.preflight_and_pause()
    originals = {
        key: copy.deepcopy(manifest.files[key])
        for key in retirement_controller.ENTRYPOINTS
    }
    collision = originals["retired-requeue-test"]
    collision_archive = (
        manifest.archive_dir
        / "retired-entrypoints"
        / collision.active_path.name
    )
    real_remove = cron_jobs.remove_job
    real_save = cron_jobs.save_jobs
    collision_created = False
    registry_restores = 0

    def fail_second_removal(job_id):
        nonlocal collision_created
        if job_id == "ff865c33c5ea":
            shutil.copy2(collision_archive, collision.active_path)
            collision_created = True
            return False
        return real_remove(job_id)

    def observe_save(jobs, **kwargs):
        nonlocal registry_restores
        if RETIRE_IDS <= {job.get("id") for job in jobs}:
            registry_restores += 1
        return real_save(jobs, **kwargs)

    monkeypatch.setattr(cron_jobs, "remove_job", fail_second_removal)
    monkeypatch.setattr(cron_jobs, "save_jobs", observe_save)

    with pytest.raises(
        RetirementError, match="exact paused-state compensation failed"
    ):
        finalize(
            retirement_fixture.paths, manifest, manifest.cutoff_epoch + 181
        )

    assert collision_created is True
    assert registry_restores == 0
    assert collision.active_path.is_file()
    assert collision_archive.is_file()
    for key, fingerprint in originals.items():
        if key != "retired-requeue-test":
            assert fingerprint.active_path.is_file()
            assert _hash_file(fingerprint.active_path) == fingerprint.sha256
    assert Manifest.load(manifest.manifest_path).phase == "paused"


@pytest.mark.parametrize("retained_drift", ("invalid", "missing"))
def test_finalize_compensation_restores_paused_retirees_before_reporting_bad_retained(
    retirement_fixture, monkeypatch, retained_drift
):
    _configure_retained_progress_job(retirement_fixture)
    manifest = retirement_fixture.preflight_and_pause()
    paused_retiring = {
        job["id"]: copy.deepcopy(job)
        for job in retirement_fixture.read_jobs()
        if job["id"] in RETIRE_IDS
    }
    real_remove = cron_jobs.remove_job
    real_save = cron_jobs.save_jobs
    retained_at_failure: dict = {}

    def fail_second_removal(job_id):
        if job_id != "ff865c33c5ea":
            return real_remove(job_id)
        jobs = cron_jobs.load_jobs()
        retained = next(job for job in jobs if job["id"] == RETAIN_ID)
        if retained_drift == "invalid":
            retained["name"] = "invalid interleaved retained config"
            retained_at_failure.update(copy.deepcopy(retained))
        else:
            jobs = [job for job in jobs if job["id"] != RETAIN_ID]
        real_save(jobs)
        return False

    monkeypatch.setattr(cron_jobs, "remove_job", fail_second_removal)
    with pytest.raises(
        RetirementError, match="exact paused-state compensation failed"
    ):
        finalize(
            retirement_fixture.paths, manifest, manifest.cutoff_epoch + 181
        )

    restored = {job["id"]: job for job in retirement_fixture.read_jobs()}
    assert {job_id: restored[job_id] for job_id in RETIRE_IDS} == paused_retiring
    if retained_drift == "invalid":
        assert restored[RETAIN_ID] == retained_at_failure
    else:
        assert RETAIN_ID not in restored
    assert all(
        restored[job_id]["enabled"] is False
        and restored[job_id]["state"] == "paused"
        for job_id in RETIRE_IDS
    )


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


@pytest.mark.parametrize(
    ("table", "expect_regression"),
    (
        pytest.param("requests", False, id="rolling-requests"),
        pytest.param("counters", True, id="durable-counters"),
    ),
)
def test_boris_verification_applies_monotonic_counts_only_to_durable_tables(
    retirement_fixture, table, expect_regression
):
    manifest = retirement_fixture.complete_retirement()
    manifest = retirement_fixture.checkpoint_and_change_reaction(manifest)
    database = retirement_fixture.paths.state_dir / "boris_intake.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """CREATE TABLE requests (user_id TEXT NOT NULL, at REAL NOT NULL);
               CREATE TABLE counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
               INSERT INTO requests VALUES ('U1', 1.0), ('U1', 2.0);
               INSERT INTO counters VALUES ('accepted', 1), ('rejected', 1);"""
        )
    manifest = checkpoint_component(
        retirement_fixture.paths,
        manifest,
        "boris-intake",
        manifest.cutoff_epoch + 210,
    )
    with sqlite3.connect(database) as connection:
        connection.execute(f'DELETE FROM "{table}" WHERE rowid = 1')

    live_source = retirement_fixture.paths.live_scripts / "boris_intake"
    versioned_source = (
        retirement_fixture.paths.source_root / "cron" / "scripts" / "boris_intake"
    )
    shutil.rmtree(live_source)
    shutil.copytree(versioned_source, live_source)
    heartbeat = retirement_fixture.paths.state_dir / "boris_intake_heartbeat"
    checkpoint_mtime = manifest.component_checkpoints["boris-intake"][-1][
        "heartbeat_mtime"
    ]
    os.utime(heartbeat, (checkpoint_mtime + 5, checkpoint_mtime + 5))

    errors = verify_retired(
        retirement_fixture.paths, manifest, manifest.cutoff_epoch + 240
    )

    regression = f"Boris store row count regressed for {table}"
    assert (regression in errors) is expect_regression


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


def test_failed_pause_registry_restoration_reports_guarded_failure(
    retirement_fixture, monkeypatch
):
    manifest = retirement_fixture.preflight_only()
    real_save_jobs = cron_jobs.save_jobs

    def fail_manifest_save():
        raise OSError("injected manifest save failure")

    def fail_original_registry_restore(jobs):
        retiring = {
            job["id"]: job for job in jobs if job.get("id") in RETIRE_IDS
        }
        if set(retiring) == RETIRE_IDS and all(
            job.get("enabled") is True for job in retiring.values()
        ):
            raise OSError("injected registry restore failure")
        return real_save_jobs(jobs)

    monkeypatch.setattr(manifest, "save", fail_manifest_save)
    monkeypatch.setattr(cron_jobs, "save_jobs", fail_original_registry_restore)

    with pytest.raises(
        RetirementError,
        match="producer pause failed and exact preflight state could not be restored",
    ) as caught:
        pause_producers(
            retirement_fixture.paths,
            manifest,
            retirement_fixture.now + 1,
        )

    assert isinstance(caught.value.__cause__, OSError)
    assert str(caught.value.__cause__) == "injected registry restore failure"


def test_finalize_compensation_never_writes_enabled_retired_records(
    retirement_fixture, monkeypatch
):
    manifest = retirement_fixture.preflight_and_pause()
    real_remove = cron_jobs.remove_job
    real_save = cron_jobs.save_jobs
    enabled_writes: list[set[str]] = []

    def observe_save(jobs, **kwargs):
        enabled_writes.append(
            {
                job["id"]
                for job in jobs
                if job.get("id") in RETIRE_IDS and job.get("enabled") is True
            }
        )
        return real_save(jobs, **kwargs)

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


def test_finalize_compensates_a_one_shot_output_move_back_failure_and_can_retry(
    retirement_fixture, monkeypatch
):
    manifest = retirement_fixture.preflight_and_pause()
    paused_jobs = retirement_fixture.read_jobs()
    audit_before = retirement_fixture.audit_hashes()
    target_id = sorted(RETIRE_IDS)[0]
    output = retirement_fixture.paths.cron_output / target_id
    protected = manifest.archive_dir / "protected-output" / target_id
    real_replace = os.replace
    injected = False

    def fail_first_output_move_back(source, destination):
        nonlocal injected
        if (
            not injected
            and Path(source) == protected
            and Path(destination) == output
        ):
            injected = True
            raise OSError("injected output move-back failure")
        return real_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_first_output_move_back)

    with pytest.raises(OSError, match="injected output move-back failure"):
        finalize(
            retirement_fixture.paths, manifest, manifest.cutoff_epoch + 181
        )

    assert injected is True
    assert {
        job["id"]: job for job in retirement_fixture.read_jobs()
    } == {job["id"]: job for job in paused_jobs}
    assert retirement_fixture.audit_hashes() == audit_before
    assert all(path.exists() for path in retirement_fixture.retired_live_paths())
    assert all(
        not (manifest.archive_dir / "protected-output" / job_id).exists()
        for job_id in RETIRE_IDS
    )
    assert Manifest.load(manifest.manifest_path).phase == "paused"

    recovered = Manifest.load(manifest.manifest_path)
    finalize(
        retirement_fixture.paths, recovered, recovered.cutoff_epoch + 182
    )
    assert {job["id"] for job in retirement_fixture.read_jobs()} == {RETAIN_ID}


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
    manifest = preflight(
        paths, retirement_fixture.now, retirement_fixture.recovery_ref
    )
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


@pytest.mark.parametrize("duplicate_id", (*sorted(RETIRE_IDS), RETAIN_ID))
def test_repeated_pause_rejects_duplicate_protected_job_records(
    retirement_fixture, duplicate_id
):
    manifest = retirement_fixture.preflight_and_pause()
    jobs = retirement_fixture.read_jobs()
    jobs.append(copy.deepcopy(next(job for job in jobs if job["id"] == duplicate_id)))
    retirement_fixture.jobs = jobs
    retirement_fixture.write_jobs()
    jobs_before = retirement_fixture.paths.jobs_path.read_bytes()
    manifest_before = manifest.manifest_path.read_bytes()

    with pytest.raises(RetirementError, match="expected exactly one"):
        pause_producers(
            retirement_fixture.paths, manifest, manifest.cutoff_epoch + 600
        )

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


def test_legacy_projection_baseline_immediate_verify_uses_quiescent_snapshot(
    retirement_fixture,
):
    manifest = retirement_fixture.preflight_and_pause()
    _set_legacy_alerted_at(retirement_fixture, "captured-before-finalize")
    manifest = finalize(
        retirement_fixture.paths, manifest, manifest.cutoff_epoch + 181
    )
    retirement_fixture.insert_execution(
        RETAIN_ID,
        status="completed",
        claimed_at=retirement_fixture.utc_iso(manifest.cutoff_epoch + 200),
    )

    assert verify_retired(
        retirement_fixture.paths, manifest, manifest.cutoff_epoch + 240
    ) == []


def test_legacy_projection_baseline_backward_manifest_uses_quiescent_snapshot_without_mutation(
    retirement_fixture,
):
    manifest = retirement_fixture.preflight_and_pause()
    preflight_hash = manifest.legacy_reaction_projection_sha256
    _set_legacy_alerted_at(retirement_fixture, "captured-before-finalize")
    manifest = finalize(
        retirement_fixture.paths, manifest, manifest.cutoff_epoch + 181
    )
    manifest.legacy_reaction_projection_sha256 = preflight_hash
    manifest.save()
    backward_manifest = Manifest.load(manifest.manifest_path)
    before = backward_manifest.manifest_path.read_bytes()
    retirement_fixture.insert_execution(
        RETAIN_ID,
        status="completed",
        claimed_at=retirement_fixture.utc_iso(manifest.cutoff_epoch + 200),
    )

    errors = verify_retired(
        retirement_fixture.paths,
        backward_manifest,
        backward_manifest.cutoff_epoch + 240,
    )

    assert errors == []
    assert backward_manifest.manifest_path.read_bytes() == before


def test_legacy_projection_baseline_rejects_drift_after_quiescent_snapshot(
    retirement_fixture,
):
    manifest = retirement_fixture.preflight_and_pause()
    _set_legacy_alerted_at(retirement_fixture, "captured-before-finalize")
    manifest = finalize(
        retirement_fixture.paths, manifest, manifest.cutoff_epoch + 181
    )
    _set_legacy_alerted_at(retirement_fixture, "drift-after-finalize")
    retirement_fixture.insert_execution(
        RETAIN_ID,
        status="completed",
        claimed_at=retirement_fixture.utc_iso(manifest.cutoff_epoch + 200),
    )

    errors = verify_retired(
        retirement_fixture.paths, manifest, manifest.cutoff_epoch + 240
    )

    assert "legacy reaction projection changed" in errors


@pytest.mark.parametrize(
    ("damage", "expected_reason"),
    (
        pytest.param("missing", "snapshot is missing", id="missing"),
        pytest.param("malformed", "snapshot is malformed", id="malformed"),
        pytest.param(
            "fingerprint-tampered",
            "snapshot fingerprint mismatch",
            id="fingerprint-tampered",
        ),
    ),
)
def test_legacy_projection_baseline_quiescent_archive_fails_closed(
    retirement_fixture, damage, expected_reason
):
    manifest = retirement_fixture.complete_retirement()
    fingerprint = manifest.quiescent_files["reaction-ledger"]
    archive = fingerprint.archive_path
    if damage == "missing":
        archive.unlink()
    elif damage == "malformed":
        payload = b'{"posts":'
        archive.write_bytes(payload)
        manifest.quiescent_files["reaction-ledger"] = replace(
            fingerprint,
            sha256=hashlib.sha256(payload).hexdigest(),
            size=len(payload),
        )
    else:
        archive.write_bytes(archive.read_bytes() + b"\n")

    errors = verify_retired(
        retirement_fixture.paths, manifest, manifest.cutoff_epoch + 240
    )

    reaction_errors = [
        error for error in errors if error.startswith("reaction ledger verification failed:")
    ]
    assert len(reaction_errors) == 1
    assert "quiescent reaction-ledger" in reaction_errors[0]
    assert expected_reason in reaction_errors[0]


def test_legacy_projection_baseline_latest_checkpoint_snapshot_takes_precedence(
    retirement_fixture,
):
    manifest = retirement_fixture.complete_retirement()
    _set_legacy_alerted_at(retirement_fixture, "captured-before-checkpoint")
    manifest = _complete_reaction_checkpoint(retirement_fixture, manifest)
    checkpoint = manifest.component_checkpoints["retained-reaction"][-1]
    archived = json.loads(
        Path(checkpoint["files"]["reaction-ledger"]["archive_path"]).read_bytes()
    )
    expected_hash = _expected_legacy_projection_hash(archived)
    assert checkpoint["legacy_reaction_projection_sha256"] == expected_hash
    assert manifest.legacy_reaction_projection_sha256 == expected_hash
    checkpoint["legacy_reaction_projection_sha256"] = "f" * 64
    manifest.legacy_reaction_projection_sha256 = "0" * 64
    manifest.save()

    assert verify_retired(
        retirement_fixture.paths, manifest, manifest.cutoff_epoch + 240
    ) == []


def test_legacy_projection_baseline_rejects_drift_after_latest_checkpoint(
    retirement_fixture,
):
    manifest = retirement_fixture.complete_retirement()
    _set_legacy_alerted_at(retirement_fixture, "captured-before-checkpoint")
    manifest = _complete_reaction_checkpoint(retirement_fixture, manifest)
    _set_legacy_alerted_at(retirement_fixture, "drift-after-checkpoint")

    errors = verify_retired(
        retirement_fixture.paths, manifest, manifest.cutoff_epoch + 240
    )

    assert "legacy reaction projection changed" in errors


def test_legacy_alert_compensation_restores_only_checkpoint_alerted_at(
    retirement_fixture,
):
    manifest = _prepare_legacy_alert_compensation(retirement_fixture)
    before = json.loads(retirement_fixture.reaction_state_path.read_text())
    expected = copy.deepcopy(before)
    expected["posts"][LEGACY_PERMALINK]["alerted_at"] = 111
    boris_before = {
        permalink: record
        for permalink, record in before["posts"].items()
        if f"/{BORIS_CHANNEL}/" in permalink
    }

    report = retirement_controller.compensate_legacy_alerted_at(
        retirement_fixture.paths,
        manifest,
        LEGACY_PERMALINK,
        222,
        111,
        manifest.cutoff_epoch + 240,
        apply=True,
    )

    after = json.loads(retirement_fixture.reaction_state_path.read_text())
    assert after == expected
    assert {
        permalink: record
        for permalink, record in after["posts"].items()
        if f"/{BORIS_CHANNEL}/" in permalink
    } == boris_before
    assert report["status"] == "compensated"
    evidence_path = Path(report["evidence_path"])
    evidence = json.loads(evidence_path.read_text())
    assert evidence["permalink"] == LEGACY_PERMALINK
    assert evidence["before_alerted_at"] == 222
    assert evidence["after_alerted_at"] == 111
    for key in ("before", "after"):
        fingerprint = evidence["files"][key]
        archived = Path(fingerprint["archive_path"])
        assert archived.exists()
        assert _hash_file(archived) == fingerprint["sha256"]


def test_legacy_alert_compensation_dry_run_is_read_only(retirement_fixture):
    manifest = _prepare_legacy_alert_compensation(retirement_fixture)
    before = {
        "ledger": retirement_fixture.reaction_state_path.read_bytes(),
        "jobs": retirement_fixture.paths.jobs_path.read_bytes(),
        "manifest": manifest.manifest_path.read_bytes(),
        "archive_entries": sorted(
            str(path.relative_to(manifest.archive_dir))
            for path in manifest.archive_dir.rglob("*")
        ),
    }

    report = retirement_controller.compensate_legacy_alerted_at(
        retirement_fixture.paths,
        manifest,
        LEGACY_PERMALINK,
        222,
        111,
        manifest.cutoff_epoch + 240,
        apply=False,
    )

    assert report == {
        "apply": False,
        "status": "planned",
        "permalink": LEGACY_PERMALINK,
        "before_alerted_at": 222,
        "after_alerted_at": 111,
    }
    assert retirement_fixture.reaction_state_path.read_bytes() == before["ledger"]
    assert retirement_fixture.paths.jobs_path.read_bytes() == before["jobs"]
    assert manifest.manifest_path.read_bytes() == before["manifest"]
    assert sorted(
        str(path.relative_to(manifest.archive_dir))
        for path in manifest.archive_dir.rglob("*")
    ) == before["archive_entries"]


def test_legacy_alert_compensation_is_idempotent_at_exact_baseline(
    retirement_fixture,
):
    manifest = _prepare_legacy_alert_compensation(retirement_fixture)
    first = retirement_controller.compensate_legacy_alerted_at(
        retirement_fixture.paths,
        manifest,
        LEGACY_PERMALINK,
        222,
        111,
        manifest.cutoff_epoch + 240,
        apply=True,
    )
    after_first = retirement_fixture.reaction_state_path.read_bytes()
    evidence_dirs = sorted((manifest.archive_dir / "compensations").iterdir())

    second = retirement_controller.compensate_legacy_alerted_at(
        retirement_fixture.paths,
        manifest,
        LEGACY_PERMALINK,
        222,
        111,
        manifest.cutoff_epoch + 241,
        apply=True,
    )

    assert first["status"] == "compensated"
    assert second == {
        "apply": True,
        "status": "already_compensated",
        "permalink": LEGACY_PERMALINK,
        "before_alerted_at": 111,
        "after_alerted_at": 111,
    }
    assert retirement_fixture.reaction_state_path.read_bytes() == after_first
    assert sorted((manifest.archive_dir / "compensations").iterdir()) == evidence_dirs


def test_legacy_alert_compensation_rejects_boris_permalink(retirement_fixture):
    manifest = _prepare_legacy_alert_compensation(retirement_fixture)
    boris_permalink = next(
        permalink
        for permalink in json.loads(
            retirement_fixture.reaction_state_path.read_text()
        )["posts"]
        if f"/{BORIS_CHANNEL}/" in permalink
    )

    with pytest.raises(RetirementError, match="selected permalink is not legacy"):
        retirement_controller.compensate_legacy_alerted_at(
            retirement_fixture.paths,
            manifest,
            boris_permalink,
            222,
            111,
            manifest.cutoff_epoch + 240,
            apply=True,
        )


def test_legacy_alert_compensation_rejects_extra_legacy_drift(retirement_fixture):
    manifest = _prepare_legacy_alert_compensation(retirement_fixture)
    before = retirement_fixture.reaction_state_path.read_bytes()
    state = json.loads(before)
    state["posts"][LEGACY_PERMALINK]["opaque"]["unexpected"] = True
    retirement_fixture.reaction_state_path.write_text(json.dumps(state, indent=2))
    drifted = retirement_fixture.reaction_state_path.read_bytes()

    with pytest.raises(
        RetirementError,
        match="legacy drift is not limited to the selected alerted_at",
    ):
        retirement_controller.compensate_legacy_alerted_at(
            retirement_fixture.paths,
            manifest,
            LEGACY_PERMALINK,
            222,
            111,
            manifest.cutoff_epoch + 240,
            apply=True,
        )

    assert retirement_fixture.reaction_state_path.read_bytes() == drifted


@pytest.mark.parametrize(
    ("current", "baseline", "message"),
    (
        pytest.param(999, 111, "current alerted_at precondition failed", id="current"),
        pytest.param(222, 999, "baseline alerted_at precondition failed", id="baseline"),
    ),
)
def test_legacy_alert_compensation_rejects_wrong_explicit_values(
    retirement_fixture, current, baseline, message
):
    manifest = _prepare_legacy_alert_compensation(retirement_fixture)
    before = retirement_fixture.reaction_state_path.read_bytes()

    with pytest.raises(RetirementError, match=message):
        retirement_controller.compensate_legacy_alerted_at(
            retirement_fixture.paths,
            manifest,
            LEGACY_PERMALINK,
            current,
            baseline,
            manifest.cutoff_epoch + 240,
            apply=True,
        )

    assert retirement_fixture.reaction_state_path.read_bytes() == before


def test_legacy_alert_compensation_requires_retained_job_paused(
    retirement_fixture,
):
    manifest = _prepare_legacy_alert_compensation(retirement_fixture)
    with cron_jobs.use_cron_store(retirement_fixture.paths.hermes_home):
        cron_jobs.resume_job(RETAIN_ID)

    with pytest.raises(RetirementError, match="retained job must be paused"):
        retirement_controller.compensate_legacy_alerted_at(
            retirement_fixture.paths,
            manifest,
            LEGACY_PERMALINK,
            222,
            111,
            manifest.cutoff_epoch + 240,
            apply=True,
        )


@pytest.mark.parametrize("damage", ("missing", "changed"))
def test_legacy_alert_compensation_rejects_retired_archive_damage(
    retirement_fixture, damage
):
    manifest = _prepare_legacy_alert_compensation(retirement_fixture)
    archive = manifest.files["retired-channel-script"].archive_path
    if damage == "missing":
        archive.unlink()
    else:
        archive.write_bytes(archive.read_bytes() + b"changed")

    with pytest.raises(RetirementError, match="retired archive fingerprint mismatch"):
        retirement_controller.compensate_legacy_alerted_at(
            retirement_fixture.paths,
            manifest,
            LEGACY_PERMALINK,
            222,
            111,
            manifest.cutoff_epoch + 240,
            apply=True,
        )


def test_legacy_alert_compensation_rejects_busy_watchdog_lock(retirement_fixture):
    manifest = _prepare_legacy_alert_compensation(retirement_fixture)
    lock_path = retirement_fixture.reaction_state_path.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RetirementError, match="reaction ledger lock is busy"):
            retirement_controller.compensate_legacy_alerted_at(
                retirement_fixture.paths,
                manifest,
                LEGACY_PERMALINK,
                222,
                111,
                manifest.cutoff_epoch + 240,
                apply=True,
            )
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def test_cli_legacy_alert_compensation_dry_run_then_apply(
    retirement_fixture, capsys
):
    manifest = _prepare_legacy_alert_compensation(retirement_fixture)
    command = [
        "compensate-legacy-alerted-at",
        "--manifest",
        str(manifest.manifest_path),
        "--permalink",
        LEGACY_PERMALINK,
        "--expected-current-alerted-at",
        "222",
        "--expected-baseline-alerted-at",
        "111",
        "--now-epoch",
        str(manifest.cutoff_epoch + 240),
    ]
    before = retirement_fixture.reaction_state_path.read_bytes()

    assert controller_main(command) == 0
    assert retirement_fixture.reaction_state_path.read_bytes() == before
    dry_run = json.loads(capsys.readouterr().out)
    assert dry_run["apply"] is False
    assert dry_run["status"] == "planned"

    assert controller_main([*command, "--apply"]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["apply"] is True
    assert applied["status"] == "compensated"
    state = json.loads(retirement_fixture.reaction_state_path.read_text())
    assert state["posts"][LEGACY_PERMALINK]["alerted_at"] == 111


def test_retained_prompt_configuration_changes_only_prompt(retirement_fixture):
    _configure_retained_progress_job(retirement_fixture)
    manifest = _prepare_legacy_alert_compensation(retirement_fixture)
    before = next(
        job
        for job in retirement_fixture.read_jobs()
        if job["id"] == RETAIN_ID
    )
    expected = copy.deepcopy(before)
    expected["prompt"] = BORIS_ONLY_RETAINED_PROMPT

    report = retirement_controller.configure_retained_boris_only_prompt(
        retirement_fixture.paths,
        manifest,
        apply=True,
    )

    after = next(
        job
        for job in retirement_fixture.read_jobs()
        if job["id"] == RETAIN_ID
    )
    assert after == expected
    assert {key: value for key, value in after.items() if key != "prompt"} == {
        key: value for key, value in before.items() if key != "prompt"
    }
    assert report == {
        "apply": True,
        "status": "configured",
        "job_id": RETAIN_ID,
        "before_prompt": ALL_CHANNEL_RETAINED_PROMPT,
        "after_prompt": BORIS_ONLY_RETAINED_PROMPT,
    }


def test_retained_prompt_configuration_dry_run_is_read_only(retirement_fixture):
    manifest = _prepare_legacy_alert_compensation(retirement_fixture)
    before = {
        "jobs": retirement_fixture.paths.jobs_path.read_bytes(),
        "manifest": manifest.manifest_path.read_bytes(),
        "archive_entries": sorted(
            str(path.relative_to(manifest.archive_dir))
            for path in manifest.archive_dir.rglob("*")
        ),
    }

    report = retirement_controller.configure_retained_boris_only_prompt(
        retirement_fixture.paths,
        manifest,
        apply=False,
    )

    assert report == {
        "apply": False,
        "status": "planned",
        "job_id": RETAIN_ID,
        "before_prompt": ALL_CHANNEL_RETAINED_PROMPT,
        "after_prompt": BORIS_ONLY_RETAINED_PROMPT,
    }
    assert retirement_fixture.paths.jobs_path.read_bytes() == before["jobs"]
    assert manifest.manifest_path.read_bytes() == before["manifest"]
    assert sorted(
        str(path.relative_to(manifest.archive_dir))
        for path in manifest.archive_dir.rglob("*")
    ) == before["archive_entries"]


def test_retained_prompt_configuration_is_idempotent(retirement_fixture):
    manifest = _prepare_legacy_alert_compensation(retirement_fixture)
    first = retirement_controller.configure_retained_boris_only_prompt(
        retirement_fixture.paths,
        manifest,
        apply=True,
    )
    after_first = retirement_fixture.paths.jobs_path.read_bytes()

    second = retirement_controller.configure_retained_boris_only_prompt(
        retirement_fixture.paths,
        manifest,
        apply=True,
    )

    assert first["status"] == "configured"
    assert second == {
        "apply": True,
        "status": "already_configured",
        "job_id": RETAIN_ID,
        "before_prompt": BORIS_ONLY_RETAINED_PROMPT,
        "after_prompt": BORIS_ONLY_RETAINED_PROMPT,
    }
    assert retirement_fixture.paths.jobs_path.read_bytes() == after_first


def test_retained_prompt_configuration_rejects_unknown_current_prompt(
    retirement_fixture,
):
    manifest = _prepare_legacy_alert_compensation(retirement_fixture)
    with cron_jobs.use_cron_store(retirement_fixture.paths.hermes_home):
        with cron_jobs._jobs_lock():
            jobs = cron_jobs.load_jobs()
            next(job for job in jobs if job["id"] == RETAIN_ID)["prompt"] = (
                "operator-owned prompt"
            )
            cron_jobs.save_jobs(jobs)
    before = retirement_fixture.paths.jobs_path.read_bytes()

    with pytest.raises(RetirementError, match="retained prompt precondition failed"):
        retirement_controller.configure_retained_boris_only_prompt(
            retirement_fixture.paths,
            manifest,
            apply=True,
        )

    assert retirement_fixture.paths.jobs_path.read_bytes() == before


@pytest.mark.parametrize(
    ("field", "value"),
    (
        pytest.param("name", "wrong-name", id="name"),
        pytest.param("script", "wrong.py", id="script"),
        pytest.param(
            "schedule",
            {"kind": "cron", "expr": "*/2 * * * *"},
            id="schedule",
        ),
    ),
)
def test_retained_prompt_configuration_rejects_wrong_job_identity(
    retirement_fixture, field, value
):
    manifest = _prepare_legacy_alert_compensation(retirement_fixture)
    with cron_jobs.use_cron_store(retirement_fixture.paths.hermes_home):
        with cron_jobs._jobs_lock():
            jobs = cron_jobs.load_jobs()
            next(job for job in jobs if job["id"] == RETAIN_ID)[field] = value
            cron_jobs.save_jobs(jobs)
    before = retirement_fixture.paths.jobs_path.read_bytes()

    with pytest.raises(RetirementError, match="paused retained job fingerprint mismatch"):
        retirement_controller.configure_retained_boris_only_prompt(
            retirement_fixture.paths,
            manifest,
            apply=True,
        )

    assert retirement_fixture.paths.jobs_path.read_bytes() == before


def test_retained_prompt_configuration_requires_paused_job(retirement_fixture):
    manifest = _prepare_legacy_alert_compensation(retirement_fixture)
    with cron_jobs.use_cron_store(retirement_fixture.paths.hermes_home):
        cron_jobs.resume_job(RETAIN_ID)

    with pytest.raises(RetirementError, match="retained job must be paused"):
        retirement_controller.configure_retained_boris_only_prompt(
            retirement_fixture.paths,
            manifest,
            apply=True,
        )


def test_retained_prompt_configuration_rejects_duplicate_retained_ids(
    retirement_fixture,
):
    manifest = _prepare_legacy_alert_compensation(retirement_fixture)
    with cron_jobs.use_cron_store(retirement_fixture.paths.hermes_home):
        with cron_jobs._jobs_lock():
            jobs = cron_jobs.load_jobs()
            retained = next(job for job in jobs if job["id"] == RETAIN_ID)
            duplicate = copy.deepcopy(retained)
            duplicate["prompt"] = BORIS_ONLY_RETAINED_PROMPT
            jobs.append(duplicate)
            cron_jobs.save_jobs(jobs)
    before = retirement_fixture.paths.jobs_path.read_bytes()

    with pytest.raises(
        RetirementError,
        match="expected exactly one retained job record",
    ):
        retirement_controller.configure_retained_boris_only_prompt(
            retirement_fixture.paths,
            manifest,
            apply=True,
        )

    assert retirement_fixture.paths.jobs_path.read_bytes() == before


def test_retained_prompt_configuration_restores_after_post_replace_failure(
    retirement_fixture, monkeypatch
):
    manifest = _prepare_legacy_alert_compensation(retirement_fixture)
    original_jobs = retirement_fixture.read_jobs()
    real_save_jobs = cron_jobs.save_jobs
    injected = False

    def save_then_fail_once(jobs):
        nonlocal injected
        result = real_save_jobs(jobs)
        retained = next(job for job in jobs if job.get("id") == RETAIN_ID)
        if not injected and retained.get("prompt") == BORIS_ONLY_RETAINED_PROMPT:
            injected = True
            raise OSError("injected post-replace failure")
        return result

    monkeypatch.setattr(cron_jobs, "save_jobs", save_then_fail_once)

    with pytest.raises(OSError, match="injected post-replace failure"):
        retirement_controller.configure_retained_boris_only_prompt(
            retirement_fixture.paths,
            manifest,
            apply=True,
        )

    assert injected is True
    assert retirement_fixture.read_jobs() == original_jobs


def test_cli_retained_prompt_configuration_dry_run_then_apply(
    retirement_fixture, capsys
):
    manifest = _prepare_legacy_alert_compensation(retirement_fixture)
    command = [
        "configure-retained-prompt",
        "--manifest",
        str(manifest.manifest_path),
        "--now-epoch",
        str(manifest.cutoff_epoch + 240),
    ]
    before = retirement_fixture.paths.jobs_path.read_bytes()

    assert controller_main(command) == 0
    assert retirement_fixture.paths.jobs_path.read_bytes() == before
    assert json.loads(capsys.readouterr().out)["status"] == "planned"

    assert controller_main([*command, "--apply"]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["status"] == "configured"
    retained = next(
        job
        for job in retirement_fixture.read_jobs()
        if job["id"] == RETAIN_ID
    )
    assert retained["prompt"] == BORIS_ONLY_RETAINED_PROMPT


def test_verify_retired_requires_boris_only_prompt_after_reaction_checkpoint(
    retirement_fixture,
):
    manifest = retirement_fixture.complete_retirement()
    manifest = _complete_reaction_checkpoint(retirement_fixture, manifest)
    with cron_jobs.use_cron_store(retirement_fixture.paths.hermes_home):
        with cron_jobs._jobs_lock():
            jobs = cron_jobs.load_jobs()
            next(job for job in jobs if job["id"] == RETAIN_ID)["prompt"] = (
                ALL_CHANNEL_RETAINED_PROMPT
            )
            cron_jobs.save_jobs(jobs)

    errors = verify_retired(
        retirement_fixture.paths, manifest, manifest.cutoff_epoch + 240
    )

    assert "retained job prompt is invalid" in errors


def test_verify_retired_rejects_duplicate_retained_ids(retirement_fixture):
    manifest = retirement_fixture.complete_retirement()
    manifest = _complete_reaction_checkpoint(retirement_fixture, manifest)
    with cron_jobs.use_cron_store(retirement_fixture.paths.hermes_home):
        with cron_jobs._jobs_lock():
            jobs = cron_jobs.load_jobs()
            retained = next(job for job in jobs if job["id"] == RETAIN_ID)
            jobs.append(copy.deepcopy(retained))
            cron_jobs.save_jobs(jobs)

    errors = verify_retired(
        retirement_fixture.paths, manifest, manifest.cutoff_epoch + 240
    )

    assert "expected exactly one retained job record, found 2" in errors


def test_boris_checkpoint_requires_boris_only_retained_prompt(retirement_fixture):
    manifest = retirement_fixture.complete_retirement()
    manifest = _complete_reaction_checkpoint(retirement_fixture, manifest)
    with cron_jobs.use_cron_store(retirement_fixture.paths.hermes_home):
        with cron_jobs._jobs_lock():
            jobs = cron_jobs.load_jobs()
            next(job for job in jobs if job["id"] == RETAIN_ID)["prompt"] = (
                ALL_CHANNEL_RETAINED_PROMPT
            )
            cron_jobs.save_jobs(jobs)

    with pytest.raises(
        RetirementError,
        match="retained watchdog must be enabled and healthy before Boris checkpoint",
    ):
        checkpoint_component(
            retirement_fixture.paths,
            manifest,
            "boris-intake",
            manifest.cutoff_epoch + 240,
        )


def test_boris_checkpoint_rejects_duplicate_retained_ids(retirement_fixture):
    manifest = retirement_fixture.complete_retirement()
    manifest = _complete_reaction_checkpoint(retirement_fixture, manifest)
    with cron_jobs.use_cron_store(retirement_fixture.paths.hermes_home):
        with cron_jobs._jobs_lock():
            jobs = cron_jobs.load_jobs()
            retained = next(job for job in jobs if job["id"] == RETAIN_ID)
            jobs.append(copy.deepcopy(retained))
            cron_jobs.save_jobs(jobs)

    with pytest.raises(
        RetirementError,
        match="expected exactly one retained job record",
    ):
        checkpoint_component(
            retirement_fixture.paths,
            manifest,
            "boris-intake",
            manifest.cutoff_epoch + 240,
        )


@pytest.mark.parametrize(
    ("damage", "expected_reason"),
    (
        pytest.param("missing", "snapshot is missing", id="missing"),
        pytest.param("malformed", "snapshot is malformed", id="malformed"),
        pytest.param(
            "fingerprint-tampered",
            "snapshot fingerprint mismatch",
            id="fingerprint-tampered",
        ),
    ),
)
def test_legacy_projection_baseline_latest_checkpoint_archive_fails_closed(
    retirement_fixture, damage, expected_reason
):
    manifest = retirement_fixture.complete_retirement()
    _set_legacy_alerted_at(retirement_fixture, "captured-before-checkpoint")
    manifest = _complete_reaction_checkpoint(retirement_fixture, manifest)
    checkpoint = manifest.component_checkpoints["retained-reaction"][-1]
    fingerprint = checkpoint["files"]["reaction-ledger"]
    archive = Path(fingerprint["archive_path"])
    if damage == "missing":
        archive.unlink()
    elif damage == "malformed":
        payload = b'{"posts":'
        archive.write_bytes(payload)
        fingerprint["sha256"] = hashlib.sha256(payload).hexdigest()
        fingerprint["size"] = len(payload)
    else:
        archive.write_bytes(archive.read_bytes() + b"\n")

    errors = verify_retired(
        retirement_fixture.paths, manifest, manifest.cutoff_epoch + 240
    )

    reaction_errors = [
        error for error in errors if error.startswith("reaction ledger verification failed:")
    ]
    assert len(reaction_errors) == 1
    assert "latest retained-reaction checkpoint reaction-ledger" in reaction_errors[0]
    assert expected_reason in reaction_errors[0]


def test_legacy_projection_baseline_ignores_boris_only_active_changes(
    retirement_fixture,
):
    state = json.loads(retirement_fixture.reaction_state_path.read_text())
    boris_permalink = (
        "https://ylopo.slack.com/archives/C0BPCKPEFCG/p1111111111111111"
    )
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
    retirement_fixture.insert_execution(
        RETAIN_ID,
        status="completed",
        claimed_at=retirement_fixture.utc_iso(manifest.cutoff_epoch + 200),
    )

    errors = verify_retired(
        retirement_fixture.paths, manifest, manifest.cutoff_epoch + 240
    )

    assert errors == []


def test_legacy_projection_baseline_finalize_hash_comes_from_archived_quiescent_bytes(
    retirement_fixture, monkeypatch
):
    manifest = retirement_fixture.preflight_and_pause()
    _set_legacy_alerted_at(retirement_fixture, "captured-by-snapshot")
    real_snapshot = retirement_controller._snapshot

    def snapshot_then_change_active(source, destination):
        fingerprint = real_snapshot(source, destination)
        if source == retirement_fixture.reaction_state_path:
            _set_legacy_alerted_at(retirement_fixture, "changed-after-snapshot")
        return fingerprint

    monkeypatch.setattr(retirement_controller, "_snapshot", snapshot_then_change_active)

    manifest = finalize(
        retirement_fixture.paths, manifest, manifest.cutoff_epoch + 181
    )
    archived = json.loads(
        manifest.quiescent_files["reaction-ledger"].archive_path.read_bytes()
    )
    active = json.loads(retirement_fixture.reaction_state_path.read_bytes())
    legacy_permalink = (
        "https://ylopo.slack.com/archives/C08K8FQKX9S/p2222222222222222"
    )

    assert archived["posts"][legacy_permalink]["alerted_at"] == "captured-by-snapshot"
    assert active["posts"][legacy_permalink]["alerted_at"] == "changed-after-snapshot"
    assert (
        manifest.legacy_reaction_projection_sha256
        == _expected_legacy_projection_hash(archived)
    )
    persisted = Manifest.load(manifest.manifest_path)
    assert (
        persisted.legacy_reaction_projection_sha256
        == _expected_legacy_projection_hash(archived)
    )


@pytest.mark.parametrize(
    "replacement",
    (
        pytest.param(True, id="integer-to-boolean"),
        pytest.param(1.0, id="integer-to-float"),
    ),
)
def test_legacy_projection_fix_round_1_rejects_json_type_representation_drift(
    retirement_fixture, replacement
):
    manifest = retirement_fixture.complete_retirement()
    state = json.loads(retirement_fixture.reaction_state_path.read_text())
    legacy_permalink = (
        "https://ylopo.slack.com/archives/C08K8FQKX9S/p2222222222222222"
    )
    assert state["posts"][legacy_permalink]["opaque"]["preserve"][0] == 1
    state["posts"][legacy_permalink]["opaque"]["preserve"][0] = replacement
    retirement_fixture.reaction_state_path.write_text(json.dumps(state, indent=2))

    errors = verify_retired(
        retirement_fixture.paths, manifest, manifest.cutoff_epoch + 240
    )

    assert errors == ["legacy reaction projection changed"]


def test_legacy_projection_fix_round_1_fails_if_archive_path_changes_during_verified_read(
    retirement_fixture, monkeypatch
):
    manifest = retirement_fixture.complete_retirement()
    fingerprint = manifest.quiescent_files["reaction-ledger"]
    archive = fingerprint.archive_path
    replacement = archive.with_name("reaction-ledger-race-replacement")
    replacement_state = json.loads(archive.read_bytes())
    legacy_permalink = (
        "https://ylopo.slack.com/archives/C08K8FQKX9S/p2222222222222222"
    )
    replacement_state["posts"][legacy_permalink]["alerted_at"] = (
        "unvalidated-replacement"
    )
    replacement_payload = json.dumps(replacement_state, indent=2).encode()
    replacement.write_bytes(replacement_payload)
    replacement.chmod(fingerprint.mode)
    retirement_fixture.reaction_state_path.write_bytes(replacement_payload)

    real_path_open = Path.open
    real_os_open = os.open
    archive_open_count = 0
    replaced = False

    def replace_archive_path() -> None:
        nonlocal replaced
        if not replaced:
            os.replace(replacement, archive)
            replaced = True

    def racing_path_open(path, *args, **kwargs):
        nonlocal archive_open_count
        if path == archive:
            archive_open_count += 1
            if archive_open_count == 2:
                replace_archive_path()
        return real_path_open(path, *args, **kwargs)

    def racing_os_open(path, flags, mode=0o777, *, dir_fd=None):
        fd = real_os_open(path, flags, mode, dir_fd=dir_fd)
        if isinstance(path, (str, os.PathLike)) and Path(path) == archive:
            replace_archive_path()
        return fd

    monkeypatch.setattr(Path, "open", racing_path_open)
    monkeypatch.setattr(os, "open", racing_os_open)

    errors = verify_retired(
        retirement_fixture.paths, manifest, manifest.cutoff_epoch + 240
    )

    assert any("snapshot changed during verified read" in error for error in errors)


def test_legacy_projection_fix_round_1_uses_second_of_two_completed_checkpoints(
    retirement_fixture,
):
    manifest = retirement_fixture.complete_retirement()
    _set_legacy_alerted_at(retirement_fixture, "first-checkpoint")
    manifest = _complete_reaction_checkpoint(
        retirement_fixture, manifest, offset=200
    )
    _set_legacy_alerted_at(retirement_fixture, "second-checkpoint")
    manifest = _complete_reaction_checkpoint(
        retirement_fixture, manifest, offset=210
    )
    checkpoints = manifest.component_checkpoints["retained-reaction"]
    first = json.loads(
        Path(checkpoints[0]["files"]["reaction-ledger"]["archive_path"]).read_bytes()
    )
    second = json.loads(
        Path(checkpoints[1]["files"]["reaction-ledger"]["archive_path"]).read_bytes()
    )
    legacy_permalink = (
        "https://ylopo.slack.com/archives/C08K8FQKX9S/p2222222222222222"
    )
    assert first["posts"][legacy_permalink]["alerted_at"] == "first-checkpoint"
    assert second["posts"][legacy_permalink]["alerted_at"] == "second-checkpoint"
    manifest.legacy_reaction_projection_sha256 = "0" * 64
    checkpoints[0]["legacy_reaction_projection_sha256"] = "1" * 64
    checkpoints[1]["legacy_reaction_projection_sha256"] = "2" * 64

    assert verify_retired(
        retirement_fixture.paths, manifest, manifest.cutoff_epoch + 240
    ) == []


@pytest.mark.parametrize("shape", ("symlink", "directory"))
def test_legacy_projection_fix_round_1_rejects_non_regular_reaction_archive(
    retirement_fixture, shape
):
    manifest = retirement_fixture.complete_retirement()
    fingerprint = manifest.quiescent_files["reaction-ledger"]
    archive = fingerprint.archive_path
    if shape == "symlink":
        target = archive.with_name("reaction-ledger-symlink-target")
        target.write_bytes(archive.read_bytes())
        target.chmod(fingerprint.mode)
        archive.unlink()
        archive.symlink_to(target)
    else:
        archive.unlink()
        archive.mkdir()

    errors = verify_retired(
        retirement_fixture.paths, manifest, manifest.cutoff_epoch + 240
    )

    assert any(
        "reaction-ledger snapshot is not a regular file" in error for error in errors
    )


@pytest.mark.parametrize("dimension", ("mode", "size", "hash"))
def test_legacy_projection_fix_round_1_rejects_each_fingerprint_dimension(
    retirement_fixture, dimension
):
    manifest = retirement_fixture.complete_retirement()
    fingerprint = manifest.quiescent_files["reaction-ledger"]
    if dimension == "mode":
        fingerprint.archive_path.chmod(fingerprint.mode ^ 0o100)
    elif dimension == "size":
        manifest.quiescent_files["reaction-ledger"] = replace(
            fingerprint, size=fingerprint.size + 1
        )
    else:
        manifest.quiescent_files["reaction-ledger"] = replace(
            fingerprint, sha256="0" * 64
        )

    errors = verify_retired(
        retirement_fixture.paths, manifest, manifest.cutoff_epoch + 240
    )

    assert any(
        "reaction-ledger snapshot fingerprint mismatch" in error for error in errors
    )


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


def test_cli_requires_recovery_ref_only_for_preflight(
    retirement_fixture, capsys
):
    with pytest.raises(SystemExit) as exc_info:
        controller_main(
            [
                "preflight",
                "--hermes-home",
                str(retirement_fixture.paths.hermes_home),
                "--source-root",
                str(retirement_fixture.paths.source_root),
            ]
        )

    assert exc_info.value.code == 2
    assert "--recovery-ref" in capsys.readouterr().err

    manifest = retirement_fixture.preflight_only()
    assert controller_main(
        ["pause", "--manifest", str(manifest.manifest_path)]
    ) == 0


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
            "--recovery-ref",
            "0" * 40,
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
            "--recovery-ref",
            "0" * 40,
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
            "--recovery-ref",
            retirement_fixture.recovery_ref,
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


@pytest.mark.parametrize("duplicate_id", (*sorted(RETIRE_IDS), RETAIN_ID))
def test_completed_rollback_rejects_duplicate_protected_job_records(
    retirement_fixture, duplicate_id
):
    manifest = retirement_fixture.complete_retirement()
    manifest = rollback(
        retirement_fixture.paths, manifest, manifest.cutoff_epoch + 240
    )
    jobs = retirement_fixture.read_jobs()
    jobs.append(copy.deepcopy(next(job for job in jobs if job["id"] == duplicate_id)))
    retirement_fixture.jobs = jobs
    retirement_fixture.write_jobs()

    with pytest.raises(
        RetirementError,
        match="completed rollback no longer verifies: job record not restored exactly",
    ):
        rollback(
            retirement_fixture.paths, manifest, manifest.cutoff_epoch + 300
        )
