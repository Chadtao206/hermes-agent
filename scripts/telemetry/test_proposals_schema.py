#!/usr/bin/env python3
from __future__ import annotations

import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
INIT_SCRIPT = THIS_DIR / "init_self_improvement_db.py"


def _run_init(telemetry_root: Path) -> None:
    proc = subprocess.run(
        [sys.executable, str(INIT_SCRIPT), "--telemetry-root", str(telemetry_root)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"init_self_improvement_db.py exited {proc.returncode}\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {row[0] for row in rows}


def _schema_version(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
    return row[0] if row else None


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {row[1] for row in rows}


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        telemetry_root = Path(tmp) / "telemetry"
        _run_init(telemetry_root)

        experiments_db = telemetry_root / "experiments.db"
        conn = sqlite3.connect(experiments_db)
        try:
            tables = _tables(conn)
            assert "proposals" in tables, tables
            assert "proposal_evidence_links" in tables, tables
            assert "proposal_apply_audit" in tables, tables
            assert _schema_version(conn) == "4", _schema_version(conn)

            proposal_columns = _table_columns(conn, "proposals")
            assert "proposal_id" in proposal_columns, proposal_columns
            assert "status" in proposal_columns, proposal_columns
            assert "approve_state" not in proposal_columns, proposal_columns
            assert "packet_json" in proposal_columns, proposal_columns

            evidence_columns = _table_columns(conn, "proposal_evidence_links")
            assert {"proposal_id", "evidence_type", "evidence_ref"}.issubset(evidence_columns), evidence_columns
        finally:
            conn.close()

        # idempotency: second run should not fail and should keep schema version.
        _run_init(telemetry_root)
        conn = sqlite3.connect(experiments_db)
        try:
            assert _schema_version(conn) == "4", _schema_version(conn)
        finally:
            conn.close()

    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
