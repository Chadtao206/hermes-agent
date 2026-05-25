#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
from pathlib import Path

from common import resolve_telemetry_root

KANBAN_DB = Path.home() / ".hermes" / "kanban.db"
INIT_SCRIPT = Path(__file__).with_name("init_self_improvement_db.py")
SYNC_SCRIPT = Path(__file__).with_name("sync_kanban_to_telemetry.py")

TABLES = (
    "execution_runs",
    "run_state_events",
    "routing_decisions",
    "task_participants",
    "review_events",
    "review_findings",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill normalized v2 telemetry tables from the live kanban board.")
    parser.add_argument("--telemetry-root", help="Override telemetry root path")
    parser.add_argument("--kanban-db", default=str(KANBAN_DB), help="Path to kanban SQLite database")
    return parser.parse_args()


def table_counts(db_path: Path) -> dict[str, int]:
    conn = sqlite3.connect(db_path)
    try:
        return {name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in TABLES}
    finally:
        conn.close()


def main() -> int:
    args = parse_args()
    telemetry_root = resolve_telemetry_root(args.telemetry_root)
    events_db = telemetry_root / "events.db"

    before = table_counts(events_db) if events_db.exists() else {name: 0 for name in TABLES}

    subprocess.run([
        "python3", str(INIT_SCRIPT), "--telemetry-root", str(telemetry_root)
    ], check=True)
    subprocess.run([
        "python3", str(SYNC_SCRIPT), "--telemetry-root", str(telemetry_root), "--kanban-db", str(Path(os.path.expanduser(args.kanban_db)).resolve())
    ], check=True)

    after = table_counts(events_db)
    print(json.dumps({
        "telemetry_root": str(telemetry_root),
        "before": before,
        "after": after,
        "delta": {name: after[name] - before.get(name, 0) for name in TABLES},
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
