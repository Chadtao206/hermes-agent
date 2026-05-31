"""Read-only SQLite -> Postgres migrator for the kanban board (Phase 4).

Single-board only: refuses if kanban_db.list_boards() returns >1 board (the
PG IDENTITY ids are global across boards; multi-board remap is deferred).
Reads the source READ-ONLY; never mutates a source board DB.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sqlite3
import sys
from pathlib import Path
from typing import Optional

import psycopg

from hermes_cli import kanban_db as kb
from hermes_cli.kanban import pg_pool

# Parent-first load order (PG has no FK constraints, so order is cosmetic).
MIGRATED_TABLES: tuple[str, ...] = (
    "tasks", "task_runs", "task_events", "task_comments", "task_links",
    "kanban_notify_subs", "kanban_profile_event_subs",
    "kanban_profile_event_claims", "kanban_profile_wake_events",
)
# Reverse order for board-scoped deletes under --force.
DELETE_ORDER: tuple[str, ...] = tuple(reversed(MIGRATED_TABLES))
IDENTITY_TABLES: tuple[str, ...] = (
    "task_comments", "task_events", "task_runs", "kanban_profile_wake_events",
)
JSON_COLUMNS: dict[str, frozenset[str]] = {
    "task_events": frozenset({"payload"}),
    "task_runs": frozenset({"metadata"}),
}


class MigrationError(Exception):
    """Fatal precondition failure (multi-board, bad source data, target guard)."""


def _norm_record(rec):
    """Normalize a store record (dataclass or dict) to a dict with any
    JSON-string fields parsed, so SQLite's JSON-as-text compares equal to
    Postgres's JSONB-as-object on CONTENT (not representation)."""
    if dataclasses.is_dataclass(rec) and not isinstance(rec, type):
        d = dataclasses.asdict(rec)
    elif isinstance(rec, dict):
        d = dict(rec)
    else:
        d = dict(getattr(rec, "__dict__", {}))
    for k, v in list(d.items()):
        if isinstance(v, str):
            s = v.strip()
            if s[:1] in ("[", "{"):
                try:
                    d[k] = json.loads(v)
                except (ValueError, TypeError):
                    pass
    return d


def _norm_list(recs):
    """Normalize + order-stabilize a list of records for parity comparison."""
    return sorted((_norm_record(r) for r in recs),
                  key=lambda d: d.get("id") or 0)


def enumerate_board() -> str:
    """Return the single board slug to migrate, or raise if >1 board exists."""
    boards = kb.list_boards()
    # Defensive: list_boards() always includes the default board, so this
    # cannot fire today; kept as belt-and-suspenders.
    if not boards:
        raise MigrationError("refusing to migrate: no boards found on disk.")
    if len(boards) > 1:
        slugs = ", ".join(sorted(b["slug"] for b in boards))
        raise MigrationError(
            f"refusing to migrate: found more than one board ({slugs}). "
            "The PG schema uses global IDENTITY ids; multi-board migration is "
            "deferred. Migrate with exactly one board on disk."
        )
    return boards[0]["slug"]


def _decode_and_validate(table: str, row: dict, errors: list[str]) -> dict:
    out: dict = {}
    rid = (row.get("id") or row.get("task_id") or row.get("event_id")
           or row.get("parent_id") or "<unknown>")
    for col, val in row.items():
        if isinstance(val, bytes):
            try:
                val = val.decode("utf-8")
            except UnicodeDecodeError:
                errors.append(f"{table}(id={rid!r}).{col}: non-utf-8 bytes")
                val = val.decode("utf-8", "replace")
        out[col] = val
    for jc in JSON_COLUMNS.get(table, ()):  # validate JSON parseability
        v = out.get(jc)
        if v in (None, ""):
            out[jc] = None
            continue
        try:
            json.loads(v)
        except (ValueError, TypeError) as e:
            errors.append(f"{table}(id={rid!r}).{jc}: invalid JSON ({e})")
    return out


def read_source(sqlite_path: Path) -> dict[str, tuple[list[str], list[dict]]]:
    """Read all 9 migrated tables READ-ONLY. Decode TEXT as strict UTF-8 and
    validate JSON columns; collect ALL offenders and raise (no partial output)."""
    con = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    con.text_factory = bytes  # surface non-UTF-8 so we can detect it
    con.row_factory = sqlite3.Row
    errors: list[str] = []
    data: dict[str, tuple[list[str], list[dict]]] = {}
    try:
        for table in MIGRATED_TABLES:
            info = con.execute(f"PRAGMA table_info({table})").fetchall()
            if not info:
                raise MigrationError(
                    f"source DB is missing table {table!r}; run the kanban DB "
                    "migrations on the source before migrating.")
            cols = [r["name"].decode() if isinstance(r["name"], bytes) else r["name"]
                    for r in info]
            rows = [
                _decode_and_validate(table, {c: r[c] for c in cols}, errors)
                for r in con.execute(f"SELECT {', '.join(cols)} FROM {table}")
            ]
            data[table] = (cols, rows)
    finally:
        con.close()
    if errors:
        raise MigrationError(
            "source contains data Postgres cannot store; scrub the source and "
            "re-run. Offenders:\n  " + "\n  ".join(errors))
    return data


def load(conn, board: str, data: dict[str, tuple[list[str], list[dict]]]) -> None:
    """Insert every migrated table's rows, stamping `board`, casting JSON->JSONB.
    Assumes conn's search_path already points at the target schema."""
    with conn.cursor() as cur:
        for table in MIGRATED_TABLES:
            cols, rows = data.get(table, ([], []))
            if not rows:
                continue
            jcols = JSON_COLUMNS.get(table, frozenset())
            placeholders = ["%s"] + [
                "%s::jsonb" if c in jcols else "%s" for c in cols]
            sql = (f"INSERT INTO {table} (board, {', '.join(cols)}) "
                   f"VALUES ({', '.join(placeholders)})")
            params = [[board] + [r.get(c) for c in cols] for r in rows]
            cur.executemany(sql, params)


def reseq(conn) -> None:
    """Set each IDENTITY sequence to max(id) so the next insert is max+1
    (or to 1/uncalled when the table is empty). Assumes conn's search_path
    already points at the target schema."""
    with conn.cursor() as cur:
        for table in IDENTITY_TABLES:
            cur.execute(
                f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                f"COALESCE((SELECT MAX(id) FROM {table}), 1), "
                f"(SELECT COUNT(*) FROM {table}) > 0)")
            cur.fetchone()


# ---------------------------------------------------------------------------
# Integrity checks — each entry is (label, SQL counting BAD rows for :board).
# The PG schema has NO FK constraints, so these orphan/dangling checks matter.
# ---------------------------------------------------------------------------
_INTEGRITY_CHECKS: tuple[tuple[str, str], ...] = (
    ("orphan task_links.parent",
     "SELECT COUNT(*) FROM task_links l WHERE l.board=%(b)s AND NOT EXISTS "
     "(SELECT 1 FROM tasks t WHERE t.board=l.board AND t.id=l.parent_id)"),
    ("orphan task_links.child",
     "SELECT COUNT(*) FROM task_links l WHERE l.board=%(b)s AND NOT EXISTS "
     "(SELECT 1 FROM tasks t WHERE t.board=l.board AND t.id=l.child_id)"),
    ("orphan task_comments.task_id",
     "SELECT COUNT(*) FROM task_comments x WHERE x.board=%(b)s AND NOT EXISTS "
     "(SELECT 1 FROM tasks t WHERE t.board=x.board AND t.id=x.task_id)"),
    ("orphan task_events.task_id",
     "SELECT COUNT(*) FROM task_events x WHERE x.board=%(b)s AND NOT EXISTS "
     "(SELECT 1 FROM tasks t WHERE t.board=x.board AND t.id=x.task_id)"),
    ("orphan task_runs.task_id",
     "SELECT COUNT(*) FROM task_runs x WHERE x.board=%(b)s AND NOT EXISTS "
     "(SELECT 1 FROM tasks t WHERE t.board=x.board AND t.id=x.task_id)"),
    ("orphan kanban_notify_subs.task_id",
     "SELECT COUNT(*) FROM kanban_notify_subs x WHERE x.board=%(b)s AND NOT EXISTS "
     "(SELECT 1 FROM tasks t WHERE t.board=x.board AND t.id=x.task_id)"),
    ("orphan kanban_profile_event_subs.task_id",
     "SELECT COUNT(*) FROM kanban_profile_event_subs x WHERE x.board=%(b)s AND NOT EXISTS "
     "(SELECT 1 FROM tasks t WHERE t.board=x.board AND t.id=x.task_id)"),
    ("orphan kanban_profile_wake_events.task_id",
     "SELECT COUNT(*) FROM kanban_profile_wake_events x WHERE x.board=%(b)s AND NOT EXISTS "
     "(SELECT 1 FROM tasks t WHERE t.board=x.board AND t.id=x.task_id)"),
    ("orphan kanban_profile_event_claims.root_task_id",
     "SELECT COUNT(*) FROM kanban_profile_event_claims x WHERE x.board=%(b)s AND NOT EXISTS "
     "(SELECT 1 FROM tasks t WHERE t.board=x.board AND t.id=x.root_task_id)"),
    ("dangling task_events.run_id",
     "SELECT COUNT(*) FROM task_events x WHERE x.board=%(b)s AND x.run_id IS NOT NULL "
     "AND NOT EXISTS (SELECT 1 FROM task_runs r WHERE r.board=x.board AND r.id=x.run_id)"),
    ("dangling tasks.current_run_id",
     "SELECT COUNT(*) FROM tasks x WHERE x.board=%(b)s AND x.current_run_id IS NOT NULL "
     "AND NOT EXISTS (SELECT 1 FROM task_runs r WHERE r.board=x.board AND r.id=x.current_run_id)"),
    ("dangling kanban_profile_event_claims.event_id",
     "SELECT COUNT(*) FROM kanban_profile_event_claims x WHERE x.board=%(b)s AND NOT EXISTS "
     "(SELECT 1 FROM task_events e WHERE e.board=x.board AND e.id=x.event_id)"),
)


@dataclasses.dataclass
class VerifyReport:
    counts: dict = dataclasses.field(default_factory=dict)        # table -> (src, tgt)
    count_mismatches: list = dataclasses.field(default_factory=list)
    integrity_failures: list = dataclasses.field(default_factory=list)
    idseq_failures: list = dataclasses.field(default_factory=list)
    parity_mismatches: list = dataclasses.field(default_factory=list)
    source_doctor_criticals: list = dataclasses.field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.count_mismatches or self.integrity_failures
                    or self.idseq_failures or self.parity_mismatches
                    or self.source_doctor_criticals)

    def render(self) -> str:
        lines = [f"verify: {'OK' if self.ok else 'FAILED'}"]
        for t, (s, g) in sorted(self.counts.items()):
            mark = "" if s == g else "  <-- MISMATCH"
            lines.append(f"  count {t:32} src={s:<7} tgt={g:<7}{mark}")
        for grp, items in (("integrity", self.integrity_failures),
                           ("id/seq", self.idseq_failures),
                           ("parity", self.parity_mismatches),
                           ("source-doctor", self.source_doctor_criticals)):
            for it in items:
                lines.append(f"  [{grp}] {it}")
        return "\n".join(lines)


def _check_counts(data, cur, board, report):
    for table in MIGRATED_TABLES:
        src = len(data.get(table, ([], []))[1])
        tgt = cur.execute(f"SELECT COUNT(*) FROM {table} WHERE board=%s",
                          (board,)).fetchone()[0]
        report.counts[table] = (src, tgt)
        if src != tgt:
            report.count_mismatches.append(f"{table}: src={src} tgt={tgt}")


def _check_integrity(cur, board, report):
    for label, sql in _INTEGRITY_CHECKS:
        bad = cur.execute(sql, {"b": board}).fetchone()[0]
        if bad:
            report.integrity_failures.append(f"{label}: {bad} bad row(s)")


def _check_idseq(data, cur, board, report):
    for table in IDENTITY_TABLES:
        rows = data.get(table, ([], []))[1]
        src_max = max((int(r["id"]) for r in rows), default=0)
        # Board-scoped: the migrated board's ids must be preserved 1:1.
        tgt_max = cur.execute(
            f"SELECT COALESCE(MAX(id),0) FROM {table} WHERE board=%s",
            (board,)).fetchone()[0]
        if tgt_max != src_max:
            report.idseq_failures.append(
                f"{table}: board max(id) src={src_max} tgt={tgt_max}")
        # The IDENTITY sequence is GLOBAL across boards: it must sit at the
        # global max(id) so the next insert never collides.
        global_max = cur.execute(
            f"SELECT COALESCE(MAX(id),0) FROM {table}").fetchone()[0]
        seq = cur.execute("SELECT pg_get_serial_sequence(%s, 'id')",
                          (table,)).fetchone()[0]
        last_value, is_called = cur.execute(
            f"SELECT last_value, is_called FROM {seq}").fetchone()
        if global_max > 0:
            if not (is_called and last_value == global_max):
                report.idseq_failures.append(
                    f"{table}: sequence last_value={last_value} "
                    f"is_called={is_called}, expected {global_max}/True")
        elif is_called:
            report.idseq_failures.append(
                f"{table}: sequence is_called=True on an empty table")


def _check_parity(data, dsn, schema, board, report, sample=None):
    """Read a sample of tasks via both stores; assert get_task() equality.
    Assumes a single board (the migration guard enforces this), so the
    unscoped list_tasks() on each store reflects only this board."""
    from hermes_cli.kanban.store_sqlite import SqliteKanbanStore
    from hermes_cli.kanban.store_postgres import PostgresKanbanStore
    task_ids = [r["id"] for r in data.get("tasks", ([], []))[1]]
    if sample is not None:
        task_ids = task_ids[:sample]
    sq = SqliteKanbanStore(board=board)
    pool = pg_pool.make_pool(dsn, search_path=f"{schema},public")
    try:
        pg = PostgresKanbanStore(board=board, pool=pool)
        sl = sq.list_tasks()
        pl = pg.list_tasks()
        if len(sl) != len(pl):
            report.parity_mismatches.append(
                f"list_tasks length src={len(sl)} tgt={len(pl)}")
        for tid in task_ids:
            a, b = sq.get_task(tid), pg.get_task(tid)
            if a != b:
                report.parity_mismatches.append(f"get_task({tid}) differs")
            for label in ("list_comments", "list_runs", "list_events"):
                if _norm_list(getattr(sq, label)(tid)) != _norm_list(getattr(pg, label)(tid)):
                    report.parity_mismatches.append(f"{label}({tid}) differs")
    finally:
        sq.close()
        pool.close()


def verify(data, dsn: str, schema: str, board: str, *,
           sqlite_path: Optional[Path] = None, sample=None,
           check_parity: bool = True) -> VerifyReport:
    """Full verification stack. `data` is the read_source() snapshot of the
    source; the target is `schema` in `dsn`. `check_parity` reads the source
    board through SqliteKanbanStore; disable it for SQL-only unit tests that
    have no matching on-disk source."""
    report = VerifyReport()
    if sqlite_path is not None:
        from hermes_cli import kanban_board_doctor as kdoc
        doc = kdoc.run_board_doctor(board=board)
        report.source_doctor_criticals = [
            i for i in doc.get("issues", []) if i.get("severity") == "critical"]
    with psycopg.connect(dsn, autocommit=True, prepare_threshold=None) as c:
        c.execute(f'SET search_path TO "{schema}", public')
        with c.cursor() as cur:
            _check_counts(data, cur, board, report)
            _check_integrity(cur, board, report)
            _check_idseq(data, cur, board, report)
    if check_parity:
        _check_parity(data, dsn, schema, board, report, sample=sample)
    return report


def _dryrun_schema_name(board: str, sqlite_path: Path) -> str:
    # Deterministic (no wall clock): board + source mtime -> stable across re-runs.
    mtime = int(sqlite_path.stat().st_mtime)
    return f"kanban_dryrun_{board}_{mtime}".replace("-", "_")[:63]


def _apply_schema(conn, schema: str) -> None:
    conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
    conn.execute(f'SET search_path TO "{schema}"')
    conn.execute(pg_pool.read_schema_ddl())


def dry_run(dsn: str, board: str, *, sqlite_path: Path):
    """Load into a throwaway schema, verify, then drop it. Returns (report, schema)."""
    data = read_source(sqlite_path)
    schema = _dryrun_schema_name(board, sqlite_path)
    with psycopg.connect(dsn, autocommit=True, prepare_threshold=None) as c:
        c.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        _apply_schema(c, schema)
        load(c, board, data)
        reseq(c)
    try:
        report = verify(data, dsn, schema, board, sqlite_path=sqlite_path)
    finally:
        with psycopg.connect(dsn, autocommit=True, prepare_threshold=None) as c:
            c.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    return report, schema


def execute(dsn: str, board: str, *, sqlite_path: Path, force: bool = False,
            target_schema: str = "public"):
    """Load into the target schema with an all-or-nothing transaction and a
    refuse-unless-force guard. Returns the VerifyReport."""
    data = read_source(sqlite_path)
    with psycopg.connect(dsn, autocommit=True, prepare_threshold=None) as c:
        _apply_schema(c, target_schema)
        existing = c.execute(
            "SELECT COUNT(*) FROM tasks WHERE board=%s", (board,)).fetchone()[0]
        if existing and not force:
            raise MigrationError(
                f"target schema {target_schema!r} already has {existing} task(s) "
                f"for board {board!r}; pass force=True/--force to overwrite.")
    with psycopg.connect(dsn, autocommit=False, prepare_threshold=None) as c:
        c.execute(f'SET search_path TO "{target_schema}"')
        if force:
            with c.cursor() as cur:
                for table in DELETE_ORDER:
                    cur.execute(f"DELETE FROM {table} WHERE board=%s", (board,))
        load(c, board, data)
        reseq(c)
        c.commit()
    report = verify(data, dsn, target_schema, board, sqlite_path=sqlite_path)
    return report


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="migrate_sqlite_to_pg",
        description="Read-only SQLite -> Postgres kanban migrator (Phase 4).")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true",
                      help="Load into a throwaway schema, verify, drop it.")
    mode.add_argument("--execute", action="store_true",
                      help="Load into the target (public) schema.")
    ap.add_argument("--force", action="store_true",
                    help="With --execute: delete the board's rows first.")
    ap.add_argument("--dsn", default=None, help="Postgres DSN (else resolve_dsn()).")
    ap.add_argument("--board", default=None, help="Board slug (else single-board guard).")
    ap.add_argument("--json", action="store_true", help="Emit a JSON report.")
    ap.add_argument("--report", default=None, help="Write the JSON report to PATH.")
    try:
        args = ap.parse_args(argv)
    except SystemExit:
        return 2

    if args.force and not args.execute:
        print("error: --force only applies to --execute", file=sys.stderr)
        return 2

    try:
        dsn = args.dsn or pg_pool.resolve_dsn()
        board = args.board or enumerate_board()
        sqlite_path = Path(kb.kanban_db_path(board))
        if args.dry_run:
            report, _schema = dry_run(dsn, board, sqlite_path=sqlite_path)
        else:
            report = execute(dsn, board, sqlite_path=sqlite_path, force=args.force)
    except (MigrationError, RuntimeError, psycopg.Error) as e:
        print(f"migration aborted: {e}", file=sys.stderr)
        return 2

    payload = {
        "ok": report.ok,
        "board": board,
        "counts": {t: {"src": s, "tgt": g} for t, (s, g) in report.counts.items()},
        "count_mismatches": report.count_mismatches,
        "integrity_failures": report.integrity_failures,
        "idseq_failures": report.idseq_failures,
        "parity_mismatches": report.parity_mismatches,
        "source_doctor_criticals": report.source_doctor_criticals,
    }
    if args.report:
        try:
            Path(args.report).write_text(json.dumps(payload, indent=2, default=str))
        except OSError as e:
            print(f"warning: could not write report to {args.report!r}: {e}",
                  file=sys.stderr)
    if args.json:
        print(json.dumps(payload, default=str))
    else:
        print(report.render())
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
