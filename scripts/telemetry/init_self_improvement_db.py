#!/usr/bin/env python3
import argparse
import os
import sqlite3
from pathlib import Path
from typing import Iterable

DEFAULT_TELEMETRY_ROOT = Path.home() / ".hermes" / "telemetry"
EVENTS_DB_NAME = "events.db"
EXPERIMENTS_DB_NAME = "experiments.db"

EVENTS_SCHEMA_VERSION = 5
EXPERIMENTS_SCHEMA_VERSION = 5

EVENTS_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS schema_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tasks (
        task_id TEXT PRIMARY KEY,
        opened_at TEXT NOT NULL,
        closed_at TEXT,
        status TEXT NOT NULL,
        surface TEXT NOT NULL,
        session_id TEXT,
        kanban_task_id TEXT,
        title TEXT NOT NULL,
        user_goal_summary TEXT NOT NULL,
        owner_profile TEXT NOT NULL,
        assisting_profiles TEXT,
        task_type TEXT,
        workdir TEXT,
        repo_hint TEXT,
        verification_strength TEXT,
        outcome TEXT,
        reopened INTEGER NOT NULL DEFAULT 0,
        final_confidence REAL,
        provenance TEXT,
        substantiality TEXT,
        created_by_profile TEXT,
        first_action_at TEXT,
        last_activity_at TEXT,
        latest_run_id TEXT,
        closeout_source TEXT,
        telemetry_complete INTEGER,
        telemetry_gaps_json TEXT,
        review_required INTEGER,
        notes_json TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS task_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        event_type TEXT NOT NULL,
        profile TEXT,
        provenance TEXT,
        payload_json TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS routing_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        initial_owner TEXT NOT NULL,
        current_owner TEXT NOT NULL,
        reroute_reason TEXT,
        ambiguity_class TEXT,
        was_initial_owner_correct INTEGER,
        final_owner TEXT,
        provenance TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS corrections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        source TEXT NOT NULL,
        profile TEXT,
        correction_type TEXT NOT NULL,
        severity TEXT NOT NULL,
        summary TEXT NOT NULL,
        provenance TEXT,
        resolved INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS learning_artifacts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        artifact_type TEXT NOT NULL,
        artifact_key TEXT NOT NULL,
        created_at TEXT NOT NULL,
        source_profile TEXT NOT NULL,
        source_task_id TEXT,
        topic TEXT,
        provenance TEXT,
        reused_count INTEGER NOT NULL DEFAULT 0,
        last_reused_at TEXT,
        contradicted INTEGER NOT NULL DEFAULT 0,
        archived INTEGER NOT NULL DEFAULT 0,
        quality_label TEXT,
        notes_json TEXT,
        UNIQUE(artifact_type, artifact_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS execution_runs (
        task_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        profile TEXT,
        status TEXT,
        outcome TEXT,
        started_at TEXT,
        ended_at TEXT,
        summary TEXT,
        error TEXT,
        metadata_json TEXT,
        source TEXT,
        PRIMARY KEY (task_id, run_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS run_state_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL,
        run_id TEXT,
        occurred_at TEXT NOT NULL,
        state TEXT NOT NULL,
        profile TEXT,
        details_json TEXT,
        source TEXT,
        UNIQUE(task_id, run_id, occurred_at, state, details_json)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS routing_decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        sequence_index INTEGER,
        initial_owner TEXT,
        decided_owner TEXT,
        final_owner TEXT,
        reason TEXT,
        ambiguity_class TEXT,
        was_initial_owner_correct INTEGER,
        evidence_source TEXT,
        source_event_id INTEGER,
        source TEXT,
        UNIQUE(task_id, sequence_index)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS task_participants (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL,
        profile TEXT NOT NULL,
        role TEXT NOT NULL,
        first_seen_at TEXT,
        last_seen_at TEXT,
        source TEXT,
        UNIQUE(task_id, profile, role)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS review_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        run_id TEXT,
        reviewer_profile TEXT,
        review_type TEXT,
        status TEXT,
        details_json TEXT,
        source TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS review_findings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL,
        review_event_id INTEGER,
        severity TEXT,
        finding_type TEXT,
        file_path TEXT,
        line INTEGER,
        summary TEXT,
        details_json TEXT,
        source TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS profile_metrics_daily (
        date TEXT NOT NULL,
        profile TEXT NOT NULL,
        sessions INTEGER,
        tasks_completed INTEGER,
        eligible_tasks INTEGER,
        excluded_tasks INTEGER,
        task_success_rate REAL,
        user_correction_rate REAL,
        routing_accuracy REAL,
        memory_growth_count INTEGER,
        memory_reuse_rate REAL,
        skill_reuse_rate REAL,
        direct_execution_share REAL,
        PRIMARY KEY (date, profile)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS bench_metrics_daily (
        date TEXT PRIMARY KEY,
        tasks_completed INTEGER,
        task_success_rate REAL,
        user_correction_rate REAL,
        reopened_task_rate REAL,
        turns_to_completion REAL,
        tool_calls_per_success REAL,
        tokens_per_success REAL,
        memory_reuse_rate REAL,
        skill_reuse_rate REAL,
        first_owner_routing_accuracy REAL,
        first_owner_routing_accuracy_sample_size INTEGER,
        first_owner_routing_accuracy_confidence TEXT,
        task_success_num INTEGER,
        task_success_den INTEGER,
        user_correction_num INTEGER,
        user_correction_den INTEGER,
        reopened_task_num INTEGER,
        reopened_task_den INTEGER,
        first_owner_routing_num INTEGER,
        first_owner_routing_den INTEGER,
        first_owner_routing_coverage_num INTEGER,
        first_owner_routing_coverage_den INTEGER,
        turns_total REAL,
        turns_n INTEGER,
        tool_calls_total REAL,
        tool_calls_n INTEGER,
        tokens_total REAL,
        tokens_n INTEGER,
        eligible_real_substantial_tasks INTEGER,
        real_lightweight_tasks INTEGER,
        bootstrap_tasks INTEGER,
        synthetic_tasks INTEGER,
        seed_tasks INTEGER,
        unknown_classification_tasks INTEGER,
        run_attempts_per_task REAL,
        failed_run_rate REAL,
        crash_rate_per_run REAL,
        give_up_rate_per_run REAL,
        median_time_to_first_action REAL,
        median_run_duration REAL,
        reroute_rate REAL,
        reviewer_engagement_rate REAL,
        review_findings_per_review REAL,
        high_severity_finding_rate REAL,
        telemetry_completeness_rate REAL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS workflow_metrics_daily (
        date TEXT NOT NULL,
        workflow TEXT NOT NULL,
        tasks_completed INTEGER,
        eligible_tasks INTEGER,
        excluded_tasks INTEGER,
        task_success_rate REAL,
        user_correction_rate REAL,
        reopened_task_rate REAL,
        avg_handoff_count REAL,
        avg_blocked_events REAL,
        avg_generic_blocked_events REAL,
        avg_review_blocked_unblocked_cycles REAL,
        avg_kanban_crashed_events REAL,
        avg_kanban_gave_up_events REAL,
        run_attempts_per_task REAL,
        failed_run_rate REAL,
        crash_rate_per_run REAL,
        give_up_rate_per_run REAL,
        reroute_rate REAL,
        reviewer_engagement_rate REAL,
        telemetry_completeness_rate REAL,
        PRIMARY KEY (date, workflow)
    )
    """,
]

EVENTS_SCHEMA_MIGRATIONS = {
    3: [
        ("bench_metrics_daily", "first_owner_routing_accuracy_sample_size INTEGER"),
        ("bench_metrics_daily", "first_owner_routing_accuracy_confidence TEXT"),
        ("workflow_metrics_daily", "avg_generic_blocked_events REAL"),
        ("workflow_metrics_daily", "avg_review_blocked_unblocked_cycles REAL"),
        ("workflow_metrics_daily", "avg_kanban_crashed_events REAL"),
        ("workflow_metrics_daily", "avg_kanban_gave_up_events REAL"),
    ],
    4: [
        ("tasks", "provenance TEXT"),
        ("tasks", "substantiality TEXT"),
        ("routing_events", "provenance TEXT"),
        ("corrections", "provenance TEXT"),
        ("task_events", "provenance TEXT"),
        ("learning_artifacts", "provenance TEXT"),
        ("profile_metrics_daily", "eligible_tasks INTEGER"),
        ("profile_metrics_daily", "excluded_tasks INTEGER"),
        ("bench_metrics_daily", "task_success_num INTEGER"),
        ("bench_metrics_daily", "task_success_den INTEGER"),
        ("bench_metrics_daily", "user_correction_num INTEGER"),
        ("bench_metrics_daily", "user_correction_den INTEGER"),
        ("bench_metrics_daily", "reopened_task_num INTEGER"),
        ("bench_metrics_daily", "reopened_task_den INTEGER"),
        ("bench_metrics_daily", "first_owner_routing_num INTEGER"),
        ("bench_metrics_daily", "first_owner_routing_den INTEGER"),
        ("bench_metrics_daily", "first_owner_routing_coverage_num INTEGER"),
        ("bench_metrics_daily", "first_owner_routing_coverage_den INTEGER"),
        ("bench_metrics_daily", "turns_total REAL"),
        ("bench_metrics_daily", "turns_n INTEGER"),
        ("bench_metrics_daily", "tool_calls_total REAL"),
        ("bench_metrics_daily", "tool_calls_n INTEGER"),
        ("bench_metrics_daily", "tokens_total REAL"),
        ("bench_metrics_daily", "tokens_n INTEGER"),
        ("bench_metrics_daily", "eligible_real_substantial_tasks INTEGER"),
        ("bench_metrics_daily", "real_lightweight_tasks INTEGER"),
        ("bench_metrics_daily", "bootstrap_tasks INTEGER"),
        ("bench_metrics_daily", "synthetic_tasks INTEGER"),
        ("bench_metrics_daily", "seed_tasks INTEGER"),
        ("bench_metrics_daily", "unknown_classification_tasks INTEGER"),
        ("workflow_metrics_daily", "eligible_tasks INTEGER"),
        ("workflow_metrics_daily", "excluded_tasks INTEGER"),
    ],
    5: [
        ("tasks", "created_by_profile TEXT"),
        ("tasks", "first_action_at TEXT"),
        ("tasks", "last_activity_at TEXT"),
        ("tasks", "latest_run_id TEXT"),
        ("tasks", "closeout_source TEXT"),
        ("tasks", "telemetry_complete INTEGER"),
        ("tasks", "telemetry_gaps_json TEXT"),
        ("tasks", "review_required INTEGER"),
        ("bench_metrics_daily", "run_attempts_per_task REAL"),
        ("bench_metrics_daily", "failed_run_rate REAL"),
        ("bench_metrics_daily", "crash_rate_per_run REAL"),
        ("bench_metrics_daily", "give_up_rate_per_run REAL"),
        ("bench_metrics_daily", "median_time_to_first_action REAL"),
        ("bench_metrics_daily", "median_run_duration REAL"),
        ("bench_metrics_daily", "reroute_rate REAL"),
        ("bench_metrics_daily", "reviewer_engagement_rate REAL"),
        ("bench_metrics_daily", "review_findings_per_review REAL"),
        ("bench_metrics_daily", "high_severity_finding_rate REAL"),
        ("bench_metrics_daily", "telemetry_completeness_rate REAL"),
        ("workflow_metrics_daily", "run_attempts_per_task REAL"),
        ("workflow_metrics_daily", "failed_run_rate REAL"),
        ("workflow_metrics_daily", "crash_rate_per_run REAL"),
        ("workflow_metrics_daily", "give_up_rate_per_run REAL"),
        ("workflow_metrics_daily", "reroute_rate REAL"),
        ("workflow_metrics_daily", "reviewer_engagement_rate REAL"),
        ("workflow_metrics_daily", "telemetry_completeness_rate REAL"),
    ],
}

EVENTS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_task_events_task_id ON task_events(task_id)",
    "CREATE INDEX IF NOT EXISTS idx_task_events_event_type ON task_events(event_type)",
    "CREATE INDEX IF NOT EXISTS idx_task_events_occurred_at ON task_events(occurred_at)",
    "CREATE INDEX IF NOT EXISTS idx_routing_events_task_id ON routing_events(task_id)",
    "CREATE INDEX IF NOT EXISTS idx_routing_events_occurred_at ON routing_events(occurred_at)",
    "CREATE INDEX IF NOT EXISTS idx_corrections_task_id ON corrections(task_id)",
    "CREATE INDEX IF NOT EXISTS idx_corrections_type ON corrections(correction_type)",
    "CREATE INDEX IF NOT EXISTS idx_corrections_occurred_at ON corrections(occurred_at)",
    "CREATE INDEX IF NOT EXISTS idx_learning_artifacts_source_task_id ON learning_artifacts(source_task_id)",
    "CREATE INDEX IF NOT EXISTS idx_learning_artifacts_created_at ON learning_artifacts(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_learning_artifacts_quality_label ON learning_artifacts(quality_label)",
    "CREATE INDEX IF NOT EXISTS idx_execution_runs_task_id ON execution_runs(task_id)",
    "CREATE INDEX IF NOT EXISTS idx_execution_runs_profile ON execution_runs(profile)",
    "CREATE INDEX IF NOT EXISTS idx_execution_runs_started_at ON execution_runs(started_at)",
    "CREATE INDEX IF NOT EXISTS idx_run_state_events_task_id ON run_state_events(task_id)",
    "CREATE INDEX IF NOT EXISTS idx_run_state_events_run_id ON run_state_events(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_run_state_events_state ON run_state_events(state)",
    "CREATE INDEX IF NOT EXISTS idx_run_state_events_occurred_at ON run_state_events(occurred_at)",
    "CREATE INDEX IF NOT EXISTS idx_routing_decisions_task_id ON routing_decisions(task_id)",
    "CREATE INDEX IF NOT EXISTS idx_routing_decisions_occurred_at ON routing_decisions(occurred_at)",
    "CREATE INDEX IF NOT EXISTS idx_routing_decisions_initial_owner ON routing_decisions(initial_owner)",
    "CREATE INDEX IF NOT EXISTS idx_task_participants_task_id ON task_participants(task_id)",
    "CREATE INDEX IF NOT EXISTS idx_task_participants_profile ON task_participants(profile)",
    "CREATE INDEX IF NOT EXISTS idx_review_events_task_id ON review_events(task_id)",
    "CREATE INDEX IF NOT EXISTS idx_review_events_occurred_at ON review_events(occurred_at)",
    "CREATE INDEX IF NOT EXISTS idx_review_findings_task_id ON review_findings(task_id)",
    "CREATE INDEX IF NOT EXISTS idx_review_findings_severity ON review_findings(severity)",
]

EXPERIMENTS_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS schema_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS experiments (
        experiment_id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        name TEXT NOT NULL,
        scope TEXT NOT NULL,
        change_summary TEXT NOT NULL,
        hypothesis TEXT NOT NULL,
        target_metrics_json TEXT NOT NULL,
        observation_window_days INTEGER NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('proposed', 'collecting_baseline', 'observing', 'ready_to_score', 'scored')),
        rollback_plan TEXT,
        owner_profile TEXT NOT NULL,
        baseline_start_date TEXT,
        baseline_end_date TEXT,
        observation_start_date TEXT,
        observation_end_date TEXT,
        min_real_tasks INTEGER,
        min_routed_tasks INTEGER,
        latest_recommendation TEXT,
        latest_scoreable_status TEXT,
        latest_not_scoreable_reasons_json TEXT,
        latest_scored_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS experiment_observations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        experiment_id TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        metric_name TEXT NOT NULL,
        metric_value REAL,
        baseline_value REAL,
        delta_value REAL,
        interpretation TEXT,
        metric_scope TEXT,
        eligible_denominator INTEGER,
        baseline_denominator INTEGER,
        confidence_label TEXT,
        scoreable_status TEXT,
        not_scoreable_reasons_json TEXT,
        window_label TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS proposals (
        proposal_id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        proposal_type TEXT NOT NULL,
        title TEXT NOT NULL,
        status TEXT NOT NULL,
        owner_profile TEXT NOT NULL,
        confidence_label TEXT NOT NULL,
        confidence_score REAL,
        confidence_basis_json TEXT NOT NULL,
        decision_requested TEXT NOT NULL,
        tl_dr TEXT NOT NULL,
        problem_statement TEXT NOT NULL,
        proposed_change TEXT NOT NULL,
        expected_metric_impact_json TEXT NOT NULL,
        risk_level TEXT NOT NULL,
        risk_notes TEXT NOT NULL,
        rollback_plan TEXT NOT NULL,
        verification_plan TEXT NOT NULL,
        approved_at TEXT,
        denied_at TEXT,
        approver TEXT,
        denial_reason TEXT,
        applied_at TEXT,
        verified_at TEXT,
        scored_at TEXT,
        outcome TEXT,
        linked_experiment_id TEXT,
        packet_json TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS proposal_evidence_links (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        proposal_id TEXT NOT NULL,
        evidence_type TEXT NOT NULL,
        evidence_ref TEXT NOT NULL,
        evidence_summary TEXT NOT NULL,
        confidence_contribution TEXT,
        created_at TEXT NOT NULL,
        UNIQUE(proposal_id, evidence_type, evidence_ref)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS proposal_decision_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        proposal_id TEXT NOT NULL,
        decided_at TEXT NOT NULL,
        decision TEXT NOT NULL,
        approver TEXT NOT NULL,
        reason TEXT,
        previous_status TEXT,
        new_status TEXT NOT NULL,
        source TEXT,
        backup_path TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS proposal_apply_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        proposal_id TEXT NOT NULL,
        applied_at TEXT NOT NULL,
        action TEXT NOT NULL,
        operator TEXT NOT NULL,
        approver TEXT,
        approval_decided_at TEXT,
        approval_source TEXT,
        reason TEXT,
        previous_status TEXT,
        new_status TEXT NOT NULL,
        source TEXT,
        idempotency_key TEXT NOT NULL,
        backup_path TEXT NOT NULL,
        kanban_backup_path TEXT,
        apply_artifact_path TEXT,
        kanban_task_id TEXT,
        manifest_path TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS proposal_outcome_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        proposal_id TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        action TEXT NOT NULL,
        operator TEXT NOT NULL,
        source TEXT,
        reason TEXT,
        kanban_task_id TEXT,
        kanban_task_status TEXT,
        kanban_completed_at TEXT,
        previous_status TEXT,
        new_status TEXT NOT NULL,
        previous_outcome TEXT,
        new_outcome TEXT,
        verified_at TEXT,
        backup_path TEXT,
        kanban_backup_path TEXT,
        manifest_path TEXT
    )
    """,
]

EXPERIMENTS_SCHEMA_MIGRATIONS = {
    2: [
        ("experiments", "baseline_start_date TEXT"),
        ("experiments", "baseline_end_date TEXT"),
        ("experiments", "observation_start_date TEXT"),
        ("experiments", "observation_end_date TEXT"),
        ("experiments", "min_real_tasks INTEGER"),
        ("experiments", "min_routed_tasks INTEGER"),
        ("experiments", "latest_recommendation TEXT"),
        ("experiments", "latest_scoreable_status TEXT"),
        ("experiments", "latest_not_scoreable_reasons_json TEXT"),
        ("experiments", "latest_scored_at TEXT"),
        ("experiment_observations", "metric_scope TEXT"),
        ("experiment_observations", "eligible_denominator INTEGER"),
        ("experiment_observations", "baseline_denominator INTEGER"),
        ("experiment_observations", "confidence_label TEXT"),
        ("experiment_observations", "scoreable_status TEXT"),
        ("experiment_observations", "not_scoreable_reasons_json TEXT"),
        ("experiment_observations", "window_label TEXT"),
    ],
    3: [],
    4: [],
    5: [],
}

EXPERIMENTS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_experiments_status ON experiments(status)",
    "CREATE INDEX IF NOT EXISTS idx_experiments_created_at ON experiments(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_experiment_observations_experiment_id ON experiment_observations(experiment_id)",
    "CREATE INDEX IF NOT EXISTS idx_experiment_observations_metric_name ON experiment_observations(metric_name)",
    "CREATE INDEX IF NOT EXISTS idx_experiment_observations_observed_at ON experiment_observations(observed_at)",
    "CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals(status)",
    "CREATE INDEX IF NOT EXISTS idx_proposals_type ON proposals(proposal_type)",
    "CREATE INDEX IF NOT EXISTS idx_proposal_evidence_links_proposal_id ON proposal_evidence_links(proposal_id)",
    "CREATE INDEX IF NOT EXISTS idx_proposal_decision_audit_proposal_id ON proposal_decision_audit(proposal_id)",
    "CREATE INDEX IF NOT EXISTS idx_proposal_apply_audit_proposal_id ON proposal_apply_audit(proposal_id)",
    "CREATE INDEX IF NOT EXISTS idx_proposal_outcome_audit_proposal_id ON proposal_outcome_audit(proposal_id)",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Initialize Hermes self-improvement telemetry databases.")
    parser.add_argument(
        "--telemetry-root",
        default=str(DEFAULT_TELEMETRY_ROOT),
        help="Directory containing telemetry databases and reports (default: ~/.hermes/telemetry)",
    )
    return parser.parse_args()


def ensure_directories(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "reports").mkdir(parents=True, exist_ok=True)
    (root / "snapshots").mkdir(parents=True, exist_ok=True)


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=OFF")
    return conn


def current_schema_version(conn: sqlite3.Connection) -> int:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    row = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
    try:
        return int(row[0]) if row and row[0] is not None else 0
    except (TypeError, ValueError):
        return 0


def set_schema_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        ("schema_version", str(version)),
    )


def table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {row[1] for row in rows}


def add_column_if_missing(conn: sqlite3.Connection, table_name: str, column_spec: str) -> None:
    column_name = column_spec.split()[0]
    if column_name in table_columns(conn, table_name):
        return
    conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_spec}")


EXPERIMENT_STATUS_VALUES = (
    "proposed",
    "collecting_baseline",
    "observing",
    "ready_to_score",
    "scored",
)


EXPERIMENTS_CANONICAL_COLUMNS = (
    "experiment_id",
    "created_at",
    "name",
    "scope",
    "change_summary",
    "hypothesis",
    "target_metrics_json",
    "observation_window_days",
    "status",
    "rollback_plan",
    "owner_profile",
    "baseline_start_date",
    "baseline_end_date",
    "observation_start_date",
    "observation_end_date",
    "min_real_tasks",
    "min_routed_tasks",
    "latest_recommendation",
    "latest_scoreable_status",
    "latest_not_scoreable_reasons_json",
    "latest_scored_at",
)


def experiments_status_check_present(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='experiments'"
    ).fetchone()
    sql = (row[0] if row else "") or ""
    required_fragments = (
        "CHECK (status IN",
        "'proposed'",
        "'collecting_baseline'",
        "'observing'",
        "'ready_to_score'",
        "'scored'",
    )
    return all(fragment in sql for fragment in required_fragments)


def rebuild_experiments_table_with_status_check(conn: sqlite3.Connection) -> None:
    legacy_table = "experiments__legacy_v1"
    conn.execute(f"DROP TABLE IF EXISTS {legacy_table}")
    conn.execute(f"ALTER TABLE experiments RENAME TO {legacy_table}")
    conn.execute(EXPERIMENTS_SCHEMA[1])

    legacy_columns = table_columns(conn, legacy_table)
    shared_columns = [column for column in EXPERIMENTS_CANONICAL_COLUMNS if column in legacy_columns and column != "status"]
    status_case = (
        "CASE "
        "WHEN status = 'running' THEN 'observing' "
        "WHEN status IN ('proposed', 'collecting_baseline', 'observing', 'ready_to_score', 'scored') THEN status "
        "ELSE 'proposed' END"
    )

    insert_columns = [*shared_columns, "status"]
    select_expressions = [*shared_columns, status_case]
    conn.execute(
        f"INSERT INTO experiments ({', '.join(insert_columns)}) "
        f"SELECT {', '.join(select_expressions)} FROM {legacy_table}"
    )
    conn.execute(f"DROP TABLE {legacy_table}")


def migrate_events_db(conn: sqlite3.Connection, target_version: int) -> None:
    current = current_schema_version(conn)
    if current >= target_version:
        return
    for version in range(current + 1, target_version + 1):
        for table_name, column_spec in EVENTS_SCHEMA_MIGRATIONS.get(version, []):
            add_column_if_missing(conn, table_name, column_spec)
    set_schema_version(conn, target_version)


def migrate_experiments_db(conn: sqlite3.Connection, target_version: int) -> None:
    current = current_schema_version(conn)
    if current < target_version:
        for version in range(current + 1, target_version + 1):
            for table_name, column_spec in EXPERIMENTS_SCHEMA_MIGRATIONS.get(version, []):
                add_column_if_missing(conn, table_name, column_spec)

    conn.execute("UPDATE experiments SET status = 'observing' WHERE status = 'running'")
    invalid_statuses = ", ".join(f"'{value}'" for value in EXPERIMENT_STATUS_VALUES)
    conn.execute(
        f"UPDATE experiments SET status = 'proposed' WHERE status NOT IN ({invalid_statuses}) OR status IS NULL"
    )
    if not experiments_status_check_present(conn):
        rebuild_experiments_table_with_status_check(conn)

    set_schema_version(conn, target_version)


def initialize_db(db_path: Path, schema_statements: Iterable[str], index_statements: Iterable[str], schema_version: int) -> None:
    with connect(db_path) as conn:
        for statement in schema_statements:
            conn.execute(statement)
        if db_path.name == EVENTS_DB_NAME:
            migrate_events_db(conn, schema_version)
        elif db_path.name == EXPERIMENTS_DB_NAME:
            migrate_experiments_db(conn, schema_version)
        else:
            set_schema_version(conn, schema_version)
        for statement in index_statements:
            conn.execute(statement)
        conn.commit()


def list_tables(db_path: Path) -> list[str]:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    return [row[0] for row in rows]


def main() -> int:
    args = parse_args()
    telemetry_root = Path(os.path.expanduser(args.telemetry_root)).resolve()
    ensure_directories(telemetry_root)

    events_db = telemetry_root / EVENTS_DB_NAME
    experiments_db = telemetry_root / EXPERIMENTS_DB_NAME

    initialize_db(events_db, EVENTS_SCHEMA, EVENTS_INDEXES, EVENTS_SCHEMA_VERSION)
    initialize_db(experiments_db, EXPERIMENTS_SCHEMA, EXPERIMENTS_INDEXES, EXPERIMENTS_SCHEMA_VERSION)

    print(f"Initialized telemetry root: {telemetry_root}")
    print(f"Events DB: {events_db}")
    print(f"  tables: {', '.join(list_tables(events_db))}")
    print(f"Experiments DB: {experiments_db}")
    print(f"  tables: {', '.join(list_tables(experiments_db))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
