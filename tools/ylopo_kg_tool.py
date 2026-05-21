#!/usr/bin/env python3
"""Hermes-native tools for local Ylopo knowledge graph queries."""

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

from tools.registry import registry, tool_error, tool_result

DEFAULT_ROOT = "/Users/ctao/code/work/ylopo"
DEFAULT_DB = "/Users/ctao/.hermes/ylopo_kg.db"
KG_SCRIPT_PATH = "/Users/ctao/.hermes/scripts/ylopo_kg.py"

MAX_LIMIT = 200
DEFAULT_DEPS_LIMIT = 25
DEFAULT_REPO_LIMIT = 100
DEFAULT_SEARCH_LIMIT = 20
DEFAULT_ROUTE_LIMIT = 25
DEFAULT_TEST_LIMIT = 25
DEFAULT_SYMBOL_LIMIT = 25
DEFAULT_IMPORT_LIMIT = 25
MAX_EXCERPT_CHARS = 320


def _clamp_limit(limit: Any, default: int) -> int:
    if limit is None:
        return default
    try:
        parsed = int(limit)
    except (TypeError, ValueError):
        raise ValueError("limit must be an integer")
    if parsed < 1:
        raise ValueError("limit must be >= 1")
    return min(parsed, MAX_LIMIT)


def _script_path() -> Path:
    return Path(KG_SCRIPT_PATH)


def _db_path(db: str | None) -> Path:
    raw = (db or DEFAULT_DB).strip()
    return Path(raw).expanduser()


def _connect_read_db(db: str | None = None) -> sqlite3.Connection:
    db_path = _db_path(db)
    if not db_path.exists():
        raise FileNotFoundError(f"KG database not found: {db_path}")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _parse_attrs(attrs_json: str | None) -> Dict[str, Any]:
    if not attrs_json:
        return {}
    try:
        parsed = json.loads(attrs_json)
    except Exception:
        return {}
    if isinstance(parsed, dict):
        return parsed
    return {}


def _trim_excerpt(text: str | None) -> str:
    if not text:
        return ""
    text = text.replace("\n", " ").strip()
    if len(text) <= MAX_EXCERPT_CHARS:
        return text
    return text[: MAX_EXCERPT_CHARS - 1] + "…"


def _build_fts_literal_query(query: str) -> str:
    terms = [term for term in query.strip().split() if term]
    if not terms:
        return '""'
    escaped_terms = ['"' + term.replace('"', '""') + '"' for term in terms]
    return " AND ".join(escaped_terms)


def _normalize_rel(path: str | None) -> str:
    return (path or "").strip().replace("\\", "/").lstrip("./")


def _package_name_from_specifier(specifier: str | None) -> str:
    spec = (specifier or "").strip()
    if spec.startswith("@"):
        parts = spec.split("/")
        return "/".join(parts[:2]) if len(parts) >= 2 else spec
    return spec.split("/")[0] if spec else ""


def _resolve_repo_key(conn: sqlite3.Connection, repo: str | None) -> str | None:
    candidate = (repo or "").strip()
    if not candidate:
        return None
    row = conn.execute("SELECT repo FROM kg_nodes WHERE node_type='repo' AND repo=? LIMIT 1", (candidate,)).fetchone()
    if row:
        return row["repo"]
    row = conn.execute(
        """
        SELECT repo FROM kg_nodes
        WHERE node_type='repo' AND (key=? OR label=? OR repo LIKE ? OR label LIKE ?)
        ORDER BY CASE WHEN label=? OR repo=? THEN 0 ELSE 1 END, repo
        LIMIT 1
        """,
        (f"repo:{candidate}", candidate, f"%{candidate}%", f"%{candidate}%", candidate, candidate),
    ).fetchone()
    return row["repo"] if row else candidate


def _count_query(conn: sqlite3.Connection, table: str, where: str, args: List[Any]) -> int:
    return int(conn.execute(f"SELECT COUNT(*) AS c FROM {table} {where}", args).fetchone()["c"])


def check_ylopo_kg_script_available() -> bool:
    return _script_path().exists()


def check_ylopo_kg_read_available() -> bool:
    # Read tools query SQLite directly. The ingest script is only required for
    # ylopo_kg_ingest, so keep read availability tied to the default DB only.
    return _db_path(None).exists()


def ylopo_kg_stats(db: str | None = None) -> str:
    try:
        conn = _connect_read_db(db)
    except FileNotFoundError as exc:
        return tool_error(str(exc))
    except Exception as exc:
        return tool_error(f"Failed to open KG database: {exc}")

    try:
        node_rows = conn.execute(
            "SELECT node_type, COUNT(*) AS c FROM kg_nodes GROUP BY node_type ORDER BY c DESC"
        ).fetchall()
        edge_rows = conn.execute(
            "SELECT edge_type, COUNT(*) AS c FROM kg_edges GROUP BY edge_type ORDER BY c DESC"
        ).fetchall()
        doc_count = int(conn.execute("SELECT COUNT(*) AS c FROM kg_docs").fetchone()["c"])
        ingest_rows = conn.execute(
            """
            SELECT repo_key, branch, head_commit, indexed_at, status
            FROM kg_ingest_state
            ORDER BY indexed_at DESC, repo_key ASC
            """
        ).fetchall()
    except sqlite3.Error as exc:
        return tool_error(f"KG stats query failed: {exc}")
    finally:
        conn.close()

    node_counts = {row["node_type"]: int(row["c"]) for row in node_rows}
    edge_counts = {row["edge_type"]: int(row["c"]) for row in edge_rows}

    total_repos = len(ingest_rows)
    success_repos = sum(1 for row in ingest_rows if row["status"] == "ok")
    failed_repos = total_repos - success_repos
    latest_indexed_at = ingest_rows[0]["indexed_at"] if ingest_rows else None

    if total_repos == 0:
        health = "empty"
    elif failed_repos > 0:
        health = "degraded"
    elif doc_count == 0:
        health = "degraded"
    else:
        health = "healthy"

    return tool_result(
        success=True,
        db=str(_db_path(db)),
        counts={
            "repos": int(node_counts.get("repo", 0)),
            "packages": int(node_counts.get("package", 0)),
            "dependencies": int(edge_counts.get("depends_on", 0)),
            "docs": doc_count,
            "nodes_total": sum(node_counts.values()),
            "edges_total": sum(edge_counts.values()),
        },
        ingest_health={
            "status": health,
            "repos_total": total_repos,
            "repos_success": success_repos,
            "repos_failed": failed_repos,
            "latest_indexed_at": latest_indexed_at,
        },
        node_types=node_counts,
        edge_types=edge_counts,
    )


def ylopo_kg_deps(package_name: str, limit: Any = None, db: str | None = None) -> str:
    package = (package_name or "").strip()
    if not package:
        return tool_error("package_name is required")

    try:
        final_limit = _clamp_limit(limit, DEFAULT_DEPS_LIMIT)
    except ValueError as exc:
        return tool_error(str(exc))

    try:
        conn = _connect_read_db(db)
    except FileNotFoundError as exc:
        return tool_error(str(exc))
    except Exception as exc:
        return tool_error(f"Failed to open KG database: {exc}")

    try:
        dst = conn.execute(
            "SELECT node_id FROM kg_nodes WHERE key = ? OR label = ? LIMIT 1",
            (f"package:{package}", package),
        ).fetchone()
        if not dst:
            return tool_error(f"Package not found in graph: {package}")

        rows = conn.execute(
            """
            SELECT
                s.node_type AS src_type,
                s.key AS src_key,
                s.label AS src_label,
                e.source_repo,
                e.source_path,
                e.source_commit,
                e.attrs_json
            FROM kg_edges e
            JOIN kg_nodes s ON s.node_id = e.src_node_id
            WHERE e.edge_type = 'depends_on' AND e.dst_node_id = ?
            ORDER BY e.source_repo ASC, s.node_type ASC, s.label ASC
            """,
            (dst["node_id"],),
        ).fetchall()
    except sqlite3.Error as exc:
        return tool_error(f"KG deps query failed: {exc}")
    finally:
        conn.close()

    deduped: Dict[str, sqlite3.Row] = {}
    for row in rows:
        dedupe_key = row["source_repo"] or row["src_key"]
        existing = deduped.get(dedupe_key)
        if not existing:
            deduped[dedupe_key] = row
            continue
        if existing["src_type"] != "package" and row["src_type"] == "package":
            deduped[dedupe_key] = row

    dependents: List[Dict[str, Any]] = []
    for row in list(deduped.values())[:final_limit]:
        attrs = _parse_attrs(row["attrs_json"])
        dependents.append(
            {
                "source_type": row["src_type"],
                "source_key": row["src_key"],
                "source_label": row["src_label"],
                "source_repo": row["source_repo"],
                "source_path": row["source_path"],
                "source_commit": row["source_commit"],
                "spec": attrs.get("spec"),
                "dep_type": attrs.get("dep_type"),
                "internal_ylopo": bool(attrs.get("internal_ylopo")),
            }
        )

    return tool_result(
        success=True,
        package_name=package,
        dependents=dependents,
        count=len(dependents),
        limit=final_limit,
        db=str(_db_path(db)),
    )


def ylopo_kg_repo(name: str, limit: Any = None, db: str | None = None) -> str:
    node_name = (name or "").strip()
    if not node_name:
        return tool_error("name is required")

    try:
        final_limit = _clamp_limit(limit, DEFAULT_REPO_LIMIT)
    except ValueError as exc:
        return tool_error(str(exc))

    try:
        conn = _connect_read_db(db)
    except FileNotFoundError as exc:
        return tool_error(str(exc))
    except Exception as exc:
        return tool_error(f"Failed to open KG database: {exc}")

    try:
        row = conn.execute(
            """
            SELECT *
            FROM kg_nodes
            WHERE key = ?
               OR key = ?
               OR label = ?
               OR repo = ?
            ORDER BY CASE WHEN key = ? OR key = ? THEN 0 ELSE 1 END, updated_at DESC
            LIMIT 1
            """,
            (
                f"repo:{node_name}",
                f"package:{node_name}",
                node_name,
                node_name,
                f"repo:{node_name}",
                f"package:{node_name}",
            ),
        ).fetchone()
        if not row:
            return tool_error(f"Node not found for name: {node_name}")

        dep_rows = conn.execute(
            """
            SELECT
                d.label AS dep_label,
                d.key AS dep_key,
                e.source_repo,
                e.source_path,
                e.source_commit,
                e.attrs_json
            FROM kg_edges e
            JOIN kg_nodes d ON d.node_id = e.dst_node_id
            WHERE e.src_node_id = ?
              AND e.edge_type = 'depends_on'
              AND (
                d.label LIKE '@ylopo/%'
                OR COALESCE(e.attrs_json, '') LIKE '%"internal_ylopo":true%'
              )
            ORDER BY d.label ASC
            LIMIT ?
            """,
            (row["node_id"], final_limit),
        ).fetchall()
    except sqlite3.Error as exc:
        return tool_error(f"KG repo query failed: {exc}")
    finally:
        conn.close()

    deps: List[Dict[str, Any]] = []
    for dep in dep_rows:
        attrs = _parse_attrs(dep["attrs_json"])
        deps.append(
            {
                "dep_label": dep["dep_label"],
                "dep_key": dep["dep_key"],
                "source_repo": dep["source_repo"],
                "source_path": dep["source_path"],
                "source_commit": dep["source_commit"],
                "dep_type": attrs.get("dep_type"),
                "spec": attrs.get("spec"),
                "internal_ylopo": bool(attrs.get("internal_ylopo")),
            }
        )

    return tool_result(
        success=True,
        db=str(_db_path(db)),
        node={
            "node_id": row["node_id"],
            "node_type": row["node_type"],
            "key": row["key"],
            "label": row["label"],
            "repo": row["repo"],
            "path": row["path"],
            "attrs": _parse_attrs(row["attrs_json"]),
        },
        internal_dependencies=deps,
        count=len(deps),
        limit=final_limit,
    )


def ylopo_kg_search(query: str, limit: Any = None, db: str | None = None) -> str:
    text = (query or "").strip()
    if not text:
        return tool_error("query is required")

    try:
        final_limit = _clamp_limit(limit, DEFAULT_SEARCH_LIMIT)
    except ValueError as exc:
        return tool_error(str(exc))

    try:
        conn = _connect_read_db(db)
    except FileNotFoundError as exc:
        return tool_error(str(exc))
    except Exception as exc:
        return tool_error(f"Failed to open KG database: {exc}")

    rows: List[sqlite3.Row]
    mode = "fts"
    try:
        rows = conn.execute(
            """
            SELECT
                d.doc_id,
                d.repo,
                d.path,
                d.commit_sha,
                d.doc_type,
                snippet(kg_docs_fts, 0, '[', ']', ' … ', 18) AS excerpt,
                bm25(kg_docs_fts) AS score
            FROM kg_docs_fts
            JOIN kg_docs d ON d.doc_id = kg_docs_fts.rowid
            WHERE kg_docs_fts MATCH ?
            ORDER BY score
            LIMIT ?
            """,
            (_build_fts_literal_query(text), final_limit),
        ).fetchall()
    except sqlite3.OperationalError:
        mode = "like"
        like = f"%{text}%"
        try:
            rows = conn.execute(
                """
                SELECT
                    d.doc_id,
                    d.repo,
                    d.path,
                    d.commit_sha,
                    d.doc_type,
                    substr(d.content, 1, 240) AS excerpt,
                    0.0 AS score
                FROM kg_docs d
                WHERE d.content LIKE ?
                   OR COALESCE(d.repo, '') LIKE ?
                   OR COALESCE(d.path, '') LIKE ?
                   OR COALESCE(d.doc_type, '') LIKE ?
                ORDER BY d.updated_at DESC
                LIMIT ?
                """,
                (like, like, like, like, final_limit),
            ).fetchall()
        except sqlite3.Error as exc:
            return tool_error(f"KG search query failed: {exc}")
    except sqlite3.Error as exc:
        return tool_error(f"KG search query failed: {exc}")
    finally:
        conn.close()

    matches = [
        {
            "doc_id": row["doc_id"],
            "repo": row["repo"],
            "path": row["path"],
            "commit": row["commit_sha"],
            "doc_type": row["doc_type"],
            "excerpt": _trim_excerpt(row["excerpt"]),
            "score": float(row["score"]),
        }
        for row in rows
    ]

    return tool_result(
        success=True,
        query=text,
        mode=mode,
        count=len(matches),
        limit=final_limit,
        matches=matches,
        db=str(_db_path(db)),
    )


def ylopo_kg_health(db: str | None = None) -> str:
    try:
        conn = _connect_read_db(db)
    except FileNotFoundError as exc:
        return tool_error(str(exc))
    except Exception as exc:
        return tool_error(f"Failed to open KG database: {exc}")
    try:
        repos_indexed = int(conn.execute("SELECT COUNT(*) AS c FROM kg_ingest_state").fetchone()["c"])
        failed_repos = int(conn.execute("SELECT COUNT(*) AS c FROM kg_ingest_state WHERE status!='ok'").fetchone()["c"])
        route_rows = int(conn.execute("SELECT COUNT(*) AS c FROM kg_routes").fetchone()["c"])
        test_rows = int(conn.execute("SELECT COUNT(*) AS c FROM kg_tests").fetchone()["c"])
        symbol_rows = int(conn.execute("SELECT COUNT(*) AS c FROM kg_symbols").fetchone()["c"])
        import_rows = int(conn.execute("SELECT COUNT(*) AS c FROM kg_imports").fetchone()["c"])
        file_rows = int(conn.execute("SELECT COUNT(*) AS c FROM kg_files WHERE tombstoned=0").fetchone()["c"])
        route_nodes = int(conn.execute("SELECT COUNT(*) AS c FROM kg_nodes WHERE node_type='route'").fetchone()["c"])
        test_nodes = int(conn.execute("SELECT COUNT(*) AS c FROM kg_nodes WHERE node_type='test'").fetchone()["c"])
        route_missing_provenance = int(conn.execute("SELECT COUNT(*) AS c FROM kg_routes WHERE repo IS NULL OR path IS NULL OR source_commit IS NULL OR start_line IS NULL OR confidence IS NULL").fetchone()["c"])
        test_missing_provenance = int(conn.execute("SELECT COUNT(*) AS c FROM kg_tests WHERE repo IS NULL OR path IS NULL OR source_commit IS NULL OR start_line IS NULL OR confidence IS NULL").fetchone()["c"])
    except sqlite3.Error as exc:
        return tool_error(f"KG health query failed: {exc}")
    finally:
        conn.close()
    status = "ok" if failed_repos == 0 and route_rows == route_nodes and test_rows == test_nodes and route_missing_provenance == 0 and test_missing_provenance == 0 else "degraded"
    return tool_result(
        success=True,
        db=str(_db_path(db)),
        status=status,
        repos_indexed=repos_indexed,
        failed_repos=failed_repos,
        phase2d_complete=bool(status == "ok" and route_rows > 0 and test_rows > 0),
        counts={
            "active_files": file_rows,
            "imports": import_rows,
            "symbols": symbol_rows,
            "routes": route_rows,
            "route_nodes": route_nodes,
            "tests": test_rows,
            "test_nodes": test_nodes,
        },
        provenance_gaps={
            "routes_missing_provenance": route_missing_provenance,
            "tests_missing_provenance": test_missing_provenance,
        },
    )


def ylopo_kg_routes(repo: str | None = None, method: str | None = None, query: str | None = None, file: str | None = None, limit: Any = None, db: str | None = None) -> str:
    try:
        final_limit = _clamp_limit(limit, DEFAULT_ROUTE_LIMIT)
        conn = _connect_read_db(db)
    except (FileNotFoundError, ValueError) as exc:
        return tool_error(str(exc))
    except Exception as exc:
        return tool_error(f"Failed to open KG database: {exc}")
    clauses: List[str] = []
    args: List[Any] = []
    try:
        if repo:
            clauses.append("r.repo=?")
            args.append(_resolve_repo_key(conn, repo))
        if method:
            clauses.append("r.method=?")
            args.append(method.upper())
        if query:
            clauses.append("r.route_path LIKE ?")
            args.append(f"%{query}%")
        if file:
            clauses.append("r.path=?")
            args.append(_normalize_rel(file))
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        total = _count_query(conn, "kg_routes r", where, args)
        rows = conn.execute(
            f"""
            SELECT r.repo, r.path, r.method, r.route_path, r.handler_name, r.handler_symbol_key,
                   r.start_line, r.source_commit, r.confidence,
                   (SELECT COUNT(*) FROM kg_imports i WHERE i.repo=r.repo AND i.path=r.path AND (i.target_path LIKE '%service%' OR i.specifier LIKE '%service%' OR i.target_package LIKE '%service%')) AS service_imports
            FROM kg_routes r
            {where}
            ORDER BY r.repo, r.path, r.start_line
            LIMIT ?
            """,
            (*args, final_limit),
        ).fetchall()
    except sqlite3.Error as exc:
        return tool_error(f"KG routes query failed: {exc}")
    finally:
        conn.close()
    routes = [dict(row) for row in rows]
    return tool_result(success=True, db=str(_db_path(db)), matched=total, count=len(routes), limit=final_limit, unknown=(total == 0), routes=routes)


def ylopo_kg_tests(repo: str | None = None, file: str | None = None, target_file: str | None = None, framework: str | None = None, query: str | None = None, limit: Any = None, db: str | None = None) -> str:
    try:
        final_limit = _clamp_limit(limit, DEFAULT_TEST_LIMIT)
        conn = _connect_read_db(db)
    except (FileNotFoundError, ValueError) as exc:
        return tool_error(str(exc))
    except Exception as exc:
        return tool_error(f"Failed to open KG database: {exc}")
    clauses: List[str] = []
    args: List[Any] = []
    try:
        repo_key = _resolve_repo_key(conn, repo) if repo else None
        if repo_key:
            clauses.append("t.repo=?")
            args.append(repo_key)
        if file:
            if not repo_key:
                return tool_error("file requires repo so the path is unambiguous")
            clauses.append("t.path=?")
            args.append(_normalize_rel(file))
        if target_file:
            if not repo_key:
                return tool_error("target_file requires repo so the path is unambiguous")
            clauses.append("EXISTS (SELECT 1 FROM kg_imports i WHERE i.repo=t.repo AND i.path=t.path AND i.target_repo=? AND i.target_path=?)")
            args.extend([repo_key, _normalize_rel(target_file)])
        if framework:
            clauses.append("t.framework=?")
            args.append(framework)
        if query:
            clauses.append("(COALESCE(t.suite_name,'') LIKE ? OR COALESCE(t.test_name,'') LIKE ? OR t.path LIKE ?)")
            args.extend([f"%{query}%", f"%{query}%", f"%{query}%"])
        if not clauses:
            return tool_error("Provide repo, file, target_file, framework, or query")
        where = "WHERE " + " AND ".join(clauses)
        total = _count_query(conn, "kg_tests t", where, args)
        rows = conn.execute(
            f"""
            SELECT t.repo, t.path, t.framework, t.kind, t.suite_name, t.test_name,
                   t.start_line, t.source_commit, t.confidence, t.node_key
            FROM kg_tests t
            {where}
            ORDER BY t.repo, t.path, t.start_line
            LIMIT ?
            """,
            (*args, final_limit),
        ).fetchall()
    except sqlite3.Error as exc:
        return tool_error(f"KG tests query failed: {exc}")
    finally:
        conn.close()
    tests = [dict(row) for row in rows]
    return tool_result(success=True, db=str(_db_path(db)), matched=total, count=len(tests), limit=final_limit, unknown=(total == 0), tests=tests)


def ylopo_kg_symbols(name: str | None = None, repo: str | None = None, kind: str | None = None, path_contains: str | None = None, limit: Any = None, db: str | None = None) -> str:
    try:
        final_limit = _clamp_limit(limit, DEFAULT_SYMBOL_LIMIT)
        conn = _connect_read_db(db)
    except (FileNotFoundError, ValueError) as exc:
        return tool_error(str(exc))
    except Exception as exc:
        return tool_error(f"Failed to open KG database: {exc}")
    clauses: List[str] = []
    args: List[Any] = []
    try:
        if name:
            clauses.append("name=?")
            args.append(name)
        if repo:
            clauses.append("repo=?")
            args.append(_resolve_repo_key(conn, repo))
        if kind:
            clauses.append("kind=?")
            args.append(kind)
        if path_contains:
            clauses.append("path LIKE ?")
            args.append(f"%{path_contains}%")
        if not clauses:
            return tool_error("Provide name, repo, kind, or path_contains")
        where = "WHERE " + " AND ".join(clauses)
        total = _count_query(conn, "kg_symbols", where, args)
        rows = conn.execute(
            f"""
            SELECT repo, path, name, kind, exported, start_line, end_line, source_commit, confidence, node_key
            FROM kg_symbols
            {where}
            ORDER BY repo, path, start_line, name
            LIMIT ?
            """,
            (*args, final_limit),
        ).fetchall()
    except sqlite3.Error as exc:
        return tool_error(f"KG symbols query failed: {exc}")
    finally:
        conn.close()
    symbols = [dict(row) for row in rows]
    return tool_result(success=True, db=str(_db_path(db)), matched=total, count=len(symbols), limit=final_limit, unknown=(total == 0), symbols=symbols)


def ylopo_kg_imports(package: str | None = None, repo: str | None = None, file: str | None = None, target_file: str | None = None, limit: Any = None, db: str | None = None) -> str:
    try:
        final_limit = _clamp_limit(limit, DEFAULT_IMPORT_LIMIT)
        conn = _connect_read_db(db)
    except (FileNotFoundError, ValueError) as exc:
        return tool_error(str(exc))
    except Exception as exc:
        return tool_error(f"Failed to open KG database: {exc}")
    clauses: List[str] = []
    args: List[Any] = []
    try:
        repo_key = _resolve_repo_key(conn, repo) if repo else None
        if package:
            clauses.append("target_package=?")
            args.append(_package_name_from_specifier(package))
            if repo_key:
                clauses.append("repo=?")
                args.append(repo_key)
        if file:
            if not repo_key:
                return tool_error("file requires repo so the path is unambiguous")
            clauses.append("repo=? AND path=?")
            args.extend([repo_key, _normalize_rel(file)])
        if target_file:
            if not repo_key:
                return tool_error("target_file requires repo so the path is unambiguous")
            clauses.append("target_repo=? AND target_path=?")
            args.extend([repo_key, _normalize_rel(target_file)])
        if not clauses:
            return tool_error("Provide package, file, or target_file")
        where = "WHERE " + " AND ".join(f"({c})" for c in clauses)
        total = _count_query(conn, "kg_imports", where, args)
        rows = conn.execute(
            f"""
            SELECT repo, path, source_commit, specifier, import_kind, imported_names_json, is_type_only,
                   target_kind, target_package, target_repo, target_path, target_node_key, resolution, start_line
            FROM kg_imports
            {where}
            ORDER BY repo, path, start_line, specifier
            LIMIT ?
            """,
            (*args, final_limit),
        ).fetchall()
    except sqlite3.Error as exc:
        return tool_error(f"KG imports query failed: {exc}")
    finally:
        conn.close()
    imports = []
    for row in rows:
        item = dict(row)
        raw_names = item.pop("imported_names_json", None)
        try:
            item["imported_names"] = json.loads(raw_names) if raw_names else []
        except Exception:
            item["imported_names"] = []
        imports.append(item)
    return tool_result(success=True, db=str(_db_path(db)), matched=total, count=len(imports), limit=final_limit, unknown=(total == 0), imports=imports)


def ylopo_kg_ingest(root: str | None = None, db: str | None = None) -> str:
    script = _script_path()
    if not script.exists():
        return tool_error(f"Ylopo KG script not found: {script}")

    root_value = (root or DEFAULT_ROOT).strip()
    db_value = str(_db_path(db))
    cmd = [sys.executable, str(script), "ingest", "--root", root_value, "--db", db_value]

    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except Exception as exc:
        return tool_error(f"Failed to run ingest: {exc}")

    stdout_lines = [line for line in (proc.stdout or "").splitlines() if line.strip()]
    stderr_lines = [line for line in (proc.stderr or "").splitlines() if line.strip()]

    return tool_result(
        success=proc.returncode == 0,
        returncode=proc.returncode,
        command=cmd,
        root=root_value,
        db=db_value,
        stdout_tail=stdout_lines[-40:],
        stderr_tail=stderr_lines[-40:],
    )


YLOPO_KG_STATS_SCHEMA = {
    "name": "ylopo_kg_stats",
    "description": "Summarize Ylopo KG index counts (repos/packages/dependencies/docs) and ingest health.",
    "parameters": {
        "type": "object",
        "properties": {
            "db": {
                "type": "string",
                "description": f"Optional SQLite DB path. Default: {DEFAULT_DB}",
            }
        },
        "required": [],
    },
}

YLOPO_KG_DEPS_SCHEMA = {
    "name": "ylopo_kg_deps",
    "description": "List distinct dependents of a package from the local Ylopo KG.",
    "parameters": {
        "type": "object",
        "properties": {
            "package_name": {
                "type": "string",
                "description": "Package name, e.g. @ylopo/models-bookshelf",
            },
            "limit": {
                "type": "integer",
                "description": f"Max results (1-{MAX_LIMIT}). Default {DEFAULT_DEPS_LIMIT}.",
            },
            "db": {
                "type": "string",
                "description": f"Optional SQLite DB path. Default: {DEFAULT_DB}",
            },
        },
        "required": ["package_name"],
    },
}

YLOPO_KG_REPO_SCHEMA = {
    "name": "ylopo_kg_repo",
    "description": "Show a repo/package node plus internal (@ylopo/*) dependencies.",
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Repo key, repo label, or package name.",
            },
            "limit": {
                "type": "integer",
                "description": f"Max dependencies (1-{MAX_LIMIT}). Default {DEFAULT_REPO_LIMIT}.",
            },
            "db": {
                "type": "string",
                "description": f"Optional SQLite DB path. Default: {DEFAULT_DB}",
            },
        },
        "required": ["name"],
    },
}

YLOPO_KG_SEARCH_SCHEMA = {
    "name": "ylopo_kg_search",
    "description": "Search indexed KG docs (FTS with safe literal terms, fallback to LIKE).",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search text.",
            },
            "limit": {
                "type": "integer",
                "description": f"Max matches (1-{MAX_LIMIT}). Default {DEFAULT_SEARCH_LIMIT}.",
            },
            "db": {
                "type": "string",
                "description": f"Optional SQLite DB path. Default: {DEFAULT_DB}",
            },
        },
        "required": ["query"],
    },
}

YLOPO_KG_HEALTH_SCHEMA = {
    "name": "ylopo_kg_health",
    "description": "Report local Ylopo KG health, including Phase 2D route/test completion and provenance gaps.",
    "parameters": {"type": "object", "properties": {"db": {"type": "string", "description": f"Optional SQLite DB path. Default: {DEFAULT_DB}"}}, "required": []},
}

YLOPO_KG_ROUTES_SCHEMA = {
    "name": "ylopo_kg_routes",
    "description": "Query extracted Express/API routes with repo/path/commit/line provenance.",
    "parameters": {"type": "object", "properties": {"repo": {"type": "string"}, "method": {"type": "string"}, "query": {"type": "string"}, "file": {"type": "string"}, "limit": {"type": "integer"}, "db": {"type": "string"}}, "required": []},
}

YLOPO_KG_TESTS_SCHEMA = {
    "name": "ylopo_kg_tests",
    "description": "Query extracted JS/TS test suites/cases and import-evidence-backed likely coverage.",
    "parameters": {"type": "object", "properties": {"repo": {"type": "string"}, "file": {"type": "string"}, "target_file": {"type": "string"}, "framework": {"type": "string"}, "query": {"type": "string"}, "limit": {"type": "integer"}, "db": {"type": "string"}}, "required": []},
}

YLOPO_KG_SYMBOLS_SCHEMA = {
    "name": "ylopo_kg_symbols",
    "description": "Find JS/TS symbol definitions with repo/path/commit/line span provenance.",
    "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "repo": {"type": "string"}, "kind": {"type": "string"}, "path_contains": {"type": "string"}, "limit": {"type": "integer"}, "db": {"type": "string"}}, "required": []},
}

YLOPO_KG_IMPORTS_SCHEMA = {
    "name": "ylopo_kg_imports",
    "description": "Query JS/TS import graph by package, importing file, or target file with provenance.",
    "parameters": {"type": "object", "properties": {"package": {"type": "string"}, "repo": {"type": "string"}, "file": {"type": "string"}, "target_file": {"type": "string"}, "limit": {"type": "integer"}, "db": {"type": "string"}}, "required": []},
}

YLOPO_KG_INGEST_SCHEMA = {
    "name": "ylopo_kg_ingest",
    "description": "Run local Ylopo KG ingest (updates only the KG SQLite DB).",
    "parameters": {
        "type": "object",
        "properties": {
            "root": {
                "type": "string",
                "description": f"Root folder to scan. Default: {DEFAULT_ROOT}",
            },
            "db": {
                "type": "string",
                "description": f"SQLite DB path to write. Default: {DEFAULT_DB}",
            },
        },
        "required": [],
    },
}


registry.register(
    name="ylopo_kg_stats",
    toolset="ylopo_kg",
    schema=YLOPO_KG_STATS_SCHEMA,
    handler=lambda args, **kw: ylopo_kg_stats(db=args.get("db")),
    check_fn=check_ylopo_kg_read_available,
    emoji="🧠",
    max_result_size_chars=120_000,
)

registry.register(
    name="ylopo_kg_deps",
    toolset="ylopo_kg",
    schema=YLOPO_KG_DEPS_SCHEMA,
    handler=lambda args, **kw: ylopo_kg_deps(
        package_name=args.get("package_name", ""),
        limit=args.get("limit"),
        db=args.get("db"),
    ),
    check_fn=check_ylopo_kg_read_available,
    emoji="🧩",
    max_result_size_chars=120_000,
)

registry.register(
    name="ylopo_kg_repo",
    toolset="ylopo_kg",
    schema=YLOPO_KG_REPO_SCHEMA,
    handler=lambda args, **kw: ylopo_kg_repo(
        name=args.get("name", ""),
        limit=args.get("limit"),
        db=args.get("db"),
    ),
    check_fn=check_ylopo_kg_read_available,
    emoji="📦",
    max_result_size_chars=120_000,
)

registry.register(
    name="ylopo_kg_search",
    toolset="ylopo_kg",
    schema=YLOPO_KG_SEARCH_SCHEMA,
    handler=lambda args, **kw: ylopo_kg_search(
        query=args.get("query", ""),
        limit=args.get("limit"),
        db=args.get("db"),
    ),
    check_fn=check_ylopo_kg_read_available,
    emoji="🔎",
    max_result_size_chars=120_000,
)

registry.register(
    name="ylopo_kg_health",
    toolset="ylopo_kg",
    schema=YLOPO_KG_HEALTH_SCHEMA,
    handler=lambda args, **kw: ylopo_kg_health(db=args.get("db")),
    check_fn=check_ylopo_kg_read_available,
    emoji="🩺",
    max_result_size_chars=120_000,
)

registry.register(
    name="ylopo_kg_routes",
    toolset="ylopo_kg",
    schema=YLOPO_KG_ROUTES_SCHEMA,
    handler=lambda args, **kw: ylopo_kg_routes(repo=args.get("repo"), method=args.get("method"), query=args.get("query"), file=args.get("file"), limit=args.get("limit"), db=args.get("db")),
    check_fn=check_ylopo_kg_read_available,
)

registry.register(
    name="ylopo_kg_tests",
    toolset="ylopo_kg",
    schema=YLOPO_KG_TESTS_SCHEMA,
    handler=lambda args, **kw: ylopo_kg_tests(repo=args.get("repo"), file=args.get("file"), target_file=args.get("target_file"), framework=args.get("framework"), query=args.get("query"), limit=args.get("limit"), db=args.get("db")),
    check_fn=check_ylopo_kg_read_available,
)

registry.register(
    name="ylopo_kg_symbols",
    toolset="ylopo_kg",
    schema=YLOPO_KG_SYMBOLS_SCHEMA,
    handler=lambda args, **kw: ylopo_kg_symbols(name=args.get("name"), repo=args.get("repo"), kind=args.get("kind"), path_contains=args.get("path_contains"), limit=args.get("limit"), db=args.get("db")),
    check_fn=check_ylopo_kg_read_available,
)

registry.register(
    name="ylopo_kg_imports",
    toolset="ylopo_kg",
    schema=YLOPO_KG_IMPORTS_SCHEMA,
    handler=lambda args, **kw: ylopo_kg_imports(package=args.get("package"), repo=args.get("repo"), file=args.get("file"), target_file=args.get("target_file"), limit=args.get("limit"), db=args.get("db")),
    check_fn=check_ylopo_kg_read_available,
)

registry.register(
    name="ylopo_kg_ingest",
    toolset="ylopo_kg",
    schema=YLOPO_KG_INGEST_SCHEMA,
    handler=lambda args, **kw: ylopo_kg_ingest(
        root=args.get("root"),
        db=args.get("db"),
    ),
    check_fn=check_ylopo_kg_script_available,
    emoji="🛠️",
    max_result_size_chars=120_000,
)
